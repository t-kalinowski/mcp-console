//! A bounded single-line projection for redraw controls, not a terminal emulator.
//!
//! It recognizes CR, backspace, SGR styling, and erase-line CSI sequences.
//! Other controls are discarded without retaining their payloads.

const MAX_CONTROL_BYTES: usize = 64;
const MAX_STYLE_BYTES: usize = 256;
const MAX_STYLES: usize = 128;
const MAX_VISIBLE_CELLS: usize = 64 * 1024;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(super) enum PriorLine {
    #[default]
    Continue,
    Finalized,
    Replace,
}

pub(super) struct Update {
    pub(super) prior: PriorLine,
    pub(super) text: String,
    pub(super) active_suffix: Option<usize>,
}

#[derive(Default)]
pub(super) struct Stream {
    line: Line,
    parser: Parser,
    pending_carriage_return: bool,
    volatile: bool,
    projection: String,
    active_events: Vec<u64>,
    last_observation: Option<u64>,
}

#[derive(Default)]
struct Line {
    cells: Vec<Cell>,
    cursor: usize,
    styles: Vec<String>,
    current_style: usize,
}

#[derive(Clone, Copy)]
struct Cell {
    character: char,
    style: usize,
}

#[derive(Default)]
enum Parser {
    #[default]
    Ground,
    Escape,
    Csi(String),
    DiscardCsi,
    StringControl {
        escaped: bool,
    },
}

struct Collector<'a> {
    stream: &'a mut Stream,
    prior: PriorLine,
    prior_open: bool,
    stable: String,
    ordinary_active: String,
    baseline: Option<String>,
    became_volatile: bool,
}

impl Stream {
    pub(super) fn ingest(&mut self, text: &str) -> Update {
        let mut collector = Collector {
            baseline: self.volatile.then(|| self.projection.clone()),
            stream: self,
            prior: PriorLine::Continue,
            prior_open: true,
            stable: String::new(),
            ordinary_active: String::new(),
            became_volatile: false,
        };

        for character in text.chars() {
            collector.character(character);
        }
        collector.finish_update()
    }

    pub(super) fn finish(&mut self) -> Update {
        let mut collector = Collector {
            baseline: self.volatile.then(|| self.projection.clone()),
            stream: self,
            prior: PriorLine::Continue,
            prior_open: true,
            stable: String::new(),
            ordinary_active: String::new(),
            became_volatile: false,
        };
        if collector.stream.pending_carriage_return {
            collector.stream.pending_carriage_return = false;
            collector.carriage_return();
        }
        if !matches!(collector.stream.parser, Parser::Ground) {
            collector.stream.parser = Parser::Ground;
            collector.unsupported_control();
        }

        let text = if collector.stream.volatile {
            collector.prior = PriorLine::Replace;
            collector.stream.line.render()
        } else {
            collector.prior = PriorLine::Finalized;
            String::new()
        };
        collector.stream.reset_all();
        Update {
            prior: collector.prior,
            active_suffix: None,
            text,
        }
    }

    pub(super) fn take_active_events(&mut self) -> Vec<u64> {
        std::mem::take(&mut self.active_events)
    }

    pub(super) fn push_active_event(&mut self, sequence: u64) {
        self.active_events.push(sequence);
    }

    pub(super) fn observe(&mut self, sequence: u64) {
        self.last_observation = Some(sequence);
    }

    pub(super) fn last_observation(&self) -> Option<u64> {
        self.last_observation
    }

    fn reset_line(&mut self) {
        self.line.clear();
        self.pending_carriage_return = false;
        self.volatile = false;
        self.projection.clear();
    }

    fn reset_all(&mut self) {
        self.reset_line();
        self.parser = Parser::Ground;
        self.line.styles.clear();
        self.line.current_style = 0;
        self.last_observation = None;
    }
}

impl Collector<'_> {
    fn character(&mut self, character: char) {
        let parser = std::mem::take(&mut self.stream.parser);
        match parser {
            Parser::Ground => self.ground(character),
            Parser::Escape => match character {
                '[' => self.stream.parser = Parser::Csi("\x1b[".to_string()),
                ']' | 'P' | '^' | '_' => {
                    self.unsupported_control();
                    self.stream.parser = Parser::StringControl { escaped: false };
                }
                _ => self.unsupported_control(),
            },
            Parser::Csi(mut sequence) => {
                if sequence.len().saturating_add(character.len_utf8()) > MAX_CONTROL_BYTES {
                    self.unsupported_control();
                    self.stream.parser = if is_csi_final(character) {
                        Parser::Ground
                    } else {
                        Parser::DiscardCsi
                    };
                    return;
                }
                sequence.push(character);
                if is_csi_final(character) {
                    match character {
                        'm' => self.style(sequence),
                        'K' => self.erase_line(&sequence),
                        _ => self.unsupported_control(),
                    }
                } else {
                    self.stream.parser = Parser::Csi(sequence);
                }
            }
            Parser::DiscardCsi => {
                if !is_csi_final(character) {
                    self.stream.parser = Parser::DiscardCsi;
                }
            }
            Parser::StringControl { escaped } => {
                if character == '\x07' || (escaped && character == '\\') {
                    return;
                }
                self.stream.parser = Parser::StringControl {
                    escaped: character == '\x1b',
                };
            }
        }
    }

    fn ground(&mut self, character: char) {
        if self.stream.pending_carriage_return {
            self.stream.pending_carriage_return = false;
            if character == '\n' {
                self.newline("\r\n");
                return;
            }
            self.carriage_return();
        }

        match character {
            '\r' => self.stream.pending_carriage_return = true,
            '\n' => self.newline("\n"),
            '\x08' => {
                self.begin_volatile();
                self.stream.line.cursor = self.stream.line.cursor.saturating_sub(1);
            }
            '\x1b' => self.stream.parser = Parser::Escape,
            '\t' => self.write(character),
            character if character.is_control() => self.unsupported_control(),
            _ => self.write(character),
        }
    }

    fn write(&mut self, character: char) {
        self.stream.line.write(character);
        if !self.stream.volatile {
            self.ordinary_active.push(character);
        }
    }

    fn style(&mut self, sequence: String) {
        self.stream.line.style(&sequence);
        if !self.stream.volatile {
            self.ordinary_active.push_str(&sequence);
        }
    }

    fn erase_line(&mut self, sequence: &str) {
        self.begin_volatile();
        let parameter = &sequence[2..sequence.len() - 1];
        match parameter {
            "" | "0" => self.stream.line.erase_to_end(),
            "1" => self.stream.line.erase_to_start(),
            "2" => self.stream.line.erase_all(),
            _ => {}
        }
    }

    fn carriage_return(&mut self) {
        self.begin_volatile();
        self.stream.line.cursor = 0;
    }

    fn unsupported_control(&mut self) {
        self.begin_volatile();
    }

    fn begin_volatile(&mut self) {
        if !self.stream.volatile {
            self.stream.volatile = true;
            self.became_volatile = true;
            self.ordinary_active.clear();
            if self.prior_open {
                self.prior = PriorLine::Replace;
            }
        }
    }

    fn newline(&mut self, delimiter: &str) {
        if self.stream.volatile {
            if self.prior_open {
                self.prior = PriorLine::Replace;
            }
            self.stable.push_str(&self.stream.line.render());
        } else {
            if self.prior_open {
                self.prior = PriorLine::Finalized;
            }
            self.stable.push_str(&self.ordinary_active);
        }
        self.stable.push_str(delimiter);
        self.stream.reset_line();
        self.prior_open = false;
        self.ordinary_active.clear();
        self.baseline = None;
        self.became_volatile = false;
    }

    fn finish_update(mut self) -> Update {
        let active = if self.stream.volatile {
            let rendered = self.stream.line.render();
            let changed = self.became_volatile || self.baseline.as_ref() != Some(&rendered);
            if changed && self.prior_open {
                self.prior = PriorLine::Replace;
            }
            self.stream.projection = rendered.clone();
            if changed { rendered } else { String::new() }
        } else {
            std::mem::take(&mut self.ordinary_active)
        };
        let active_suffix = (!active.is_empty()).then_some(self.stable.len());
        self.stable.push_str(&active);
        Update {
            prior: self.prior,
            text: self.stable,
            active_suffix,
        }
    }
}

impl Line {
    fn clear(&mut self) {
        self.cells.clear();
        self.cursor = 0;
    }

    fn write(&mut self, character: char) {
        if self.cursor < MAX_VISIBLE_CELLS {
            while self.cells.len() < self.cursor {
                self.cells.push(Cell {
                    character: ' ',
                    style: 0,
                });
            }
            let cell = Cell {
                character,
                style: self.current_style,
            };
            if self.cursor < self.cells.len() {
                self.cells[self.cursor] = cell;
            } else {
                self.cells.push(cell);
            }
        }
        self.cursor = self.cursor.saturating_add(1);
    }

    fn style(&mut self, sequence: &str) {
        if self.styles.is_empty() {
            self.styles.push(String::new());
        }
        let parameters = &sequence[2..sequence.len() - 1];
        let resets = parameters.is_empty() || parameters.split(';').any(|value| value == "0");
        let mut style = if resets {
            String::new()
        } else {
            self.styles[self.current_style].clone()
        };
        if !parameters.is_empty() && parameters.split(';').any(|value| value != "0") {
            if style.len().saturating_add(sequence.len()) <= MAX_STYLE_BYTES {
                style.push_str(sequence);
            } else {
                style = sequence.to_string();
            }
        }
        self.current_style = self.intern_style(style);
    }

    fn intern_style(&mut self, style: String) -> usize {
        if let Some(index) = self.styles.iter().position(|candidate| candidate == &style) {
            return index;
        }
        if self.styles.len() == MAX_STYLES {
            return 0;
        }
        self.styles.push(style);
        self.styles.len() - 1
    }

    fn erase_to_end(&mut self) {
        self.cells.truncate(self.cursor.min(self.cells.len()));
    }

    fn erase_to_start(&mut self) {
        if self.cells.is_empty() {
            return;
        }
        let end = self.cursor.min(self.cells.len() - 1);
        for cell in &mut self.cells[..=end] {
            *cell = Cell {
                character: ' ',
                style: 0,
            };
        }
    }

    fn erase_all(&mut self) {
        self.cells.clear();
    }

    fn render(&self) -> String {
        let length = self
            .cells
            .iter()
            .rposition(|cell| cell.character != ' ' || cell.style != 0)
            .map_or(0, |index| index + 1);
        let mut rendered = String::new();
        let mut style = 0;
        for cell in &self.cells[..length] {
            if cell.style != style {
                if style != 0 {
                    rendered.push_str("\x1b[0m");
                }
                if cell.style != 0 {
                    rendered.push_str(&self.styles[cell.style]);
                }
                style = cell.style;
            }
            rendered.push(cell.character);
        }
        if style != 0 {
            rendered.push_str("\x1b[0m");
        }
        rendered
    }
}

fn is_csi_final(character: char) -> bool {
    ('@'..='~').contains(&character)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_crlf_and_compacts_redraw_controls() {
        let mut stream = Stream::default();
        let first = stream.ingest("ordinary\r");
        assert_eq!(first.text, "ordinary");
        let second = stream.ingest("\nold\r\x1b[");
        assert_eq!(second.prior, PriorLine::Finalized);
        assert_eq!(second.text, "\r\nold");
        let third = stream.ingest("2K\x1b[31mnewx\x08!\x1b[0m");
        assert_eq!(third.prior, PriorLine::Replace);
        assert_eq!(third.text, "\x1b[31mnew!\x1b[0m");
    }

    #[test]
    fn leaves_unchanged_volatile_state_out_of_later_updates() {
        let mut stream = Stream::default();
        assert_eq!(stream.ingest("value\r").text, "value");
        assert_eq!(stream.ingest("value").text, "value");
        assert_eq!(stream.ingest("\rvalue").text, "");
        assert_eq!(stream.finish().text, "value");
    }

    #[test]
    fn discards_unsupported_controls_without_buffering_their_payloads() {
        let mut stream = Stream::default();
        let output = stream.ingest(&format!("before\x1b]{}\x07\rvisible", "x".repeat(100_000)));
        assert_eq!(output.text, "visible");

        let output = stream.ingest(&format!("\x1b[{}zafter", "1".repeat(100_000)));
        assert_eq!(output.text, "visibleafter");
    }
}
