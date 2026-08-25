//! A bounded single-line projection for redraw controls, not a terminal emulator.
//!
//! It recognizes CR, backspace, SGR styling, and erase-line CSI sequences.
//! Other controls are discarded without retaining their payloads.

use unicode_width::UnicodeWidthChar;

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
    dirty: bool,
    projection: String,
    active_events: Vec<u64>,
    last_observation: Option<u64>,
}

#[derive(Default)]
struct Line {
    columns: Vec<Column>,
    cursor: usize,
    styles: Vec<String>,
    current_style: usize,
}

enum Column {
    Empty,
    Glyph(Cell),
    Continuation,
}

struct Cell {
    character: char,
    combining: Option<Box<str>>,
    style: usize,
    width: usize,
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
}

impl Stream {
    pub(super) fn ingest(&mut self, text: &str) -> Update {
        let mut collector = Collector {
            stream: self,
            prior: PriorLine::Continue,
            prior_open: true,
            stable: String::new(),
            ordinary_active: String::new(),
        };

        for character in text.chars() {
            collector.character(character);
        }
        collector.finish_update()
    }

    pub(super) fn snapshot(&mut self) -> Update {
        if !self.volatile || !self.dirty {
            return Update {
                prior: PriorLine::Continue,
                text: String::new(),
                active_suffix: None,
            };
        }

        let rendered = self.line.render();
        self.dirty = false;
        if rendered == self.projection {
            return Update {
                prior: PriorLine::Continue,
                text: String::new(),
                active_suffix: None,
            };
        }
        self.projection.clone_from(&rendered);
        Update {
            prior: PriorLine::Replace,
            active_suffix: (!rendered.is_empty()).then_some(0),
            text: rendered,
        }
    }

    pub(super) fn finish(&mut self) -> Update {
        let mut collector = Collector {
            stream: self,
            prior: PriorLine::Continue,
            prior_open: true,
            stable: String::new(),
            ordinary_active: String::new(),
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
            let rendered = collector.stream.line.render();
            if !collector.stream.active_events.is_empty() && rendered == collector.stream.projection
            {
                collector.prior = PriorLine::Finalized;
                String::new()
            } else {
                collector.prior = PriorLine::Replace;
                rendered
            }
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
        self.dirty = false;
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
        if self.stream.volatile {
            self.stream.dirty = true;
        } else {
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
        self.stream.dirty = true;
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
            self.stream.dirty = true;
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
    }

    fn finish_update(mut self) -> Update {
        let active = if self.stream.volatile && !self.stable.is_empty() {
            let rendered = self.stream.line.render();
            let changed = self.stream.dirty || rendered != self.stream.projection;
            if changed && self.prior_open {
                self.prior = PriorLine::Replace;
            }
            self.stream.projection.clone_from(&rendered);
            self.stream.dirty = false;
            if changed { rendered } else { String::new() }
        } else if self.stream.volatile {
            String::new()
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
        self.columns.clear();
        self.cursor = 0;
    }

    fn write(&mut self, character: char) {
        let width = if character == '\t' {
            1
        } else {
            character.width().unwrap_or(0)
        };
        if width == 0 {
            self.append_combining(character);
            return;
        }

        if self.cursor.saturating_add(width) <= MAX_VISIBLE_CELLS {
            self.clear_glyph_at(self.cursor);
            if width > 1 {
                self.clear_glyph_at(self.cursor + width - 1);
            }
            while self.columns.len() < self.cursor.saturating_add(width) {
                self.columns.push(Column::Empty);
            }
            self.columns[self.cursor] = Column::Glyph(Cell {
                character,
                combining: None,
                style: self.current_style,
                width,
            });
            for column in &mut self.columns[self.cursor + 1..self.cursor + width] {
                *column = Column::Continuation;
            }
        }
        self.cursor = self.cursor.saturating_add(width);
    }

    fn append_combining(&mut self, character: char) {
        let Some(mut index) = self.cursor.checked_sub(1) else {
            return;
        };
        while matches!(self.columns.get(index), Some(Column::Continuation)) {
            let Some(previous) = index.checked_sub(1) else {
                return;
            };
            index = previous;
        }
        let Some(Column::Glyph(cell)) = self.columns.get_mut(index) else {
            return;
        };
        let mut combining = cell.combining.take().map_or_else(String::new, String::from);
        combining.push(character);
        cell.combining = Some(combining.into_boxed_str());
    }

    fn clear_glyph_at(&mut self, index: usize) {
        let Some(column) = self.columns.get(index) else {
            return;
        };
        let start = match column {
            Column::Empty => return,
            Column::Glyph(_) => index,
            Column::Continuation => {
                let mut start = index;
                while start > 0 && matches!(self.columns[start], Column::Continuation) {
                    start -= 1;
                }
                start
            }
        };
        let width = match &self.columns[start] {
            Column::Glyph(cell) => cell.width,
            Column::Empty | Column::Continuation => return,
        };
        let end = (start + width).min(self.columns.len());
        for column in &mut self.columns[start..end] {
            *column = Column::Empty;
        }
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
        let mut end = self.cursor.min(self.columns.len());
        while end > 0
            && end < self.columns.len()
            && matches!(self.columns[end], Column::Continuation)
        {
            end -= 1;
        }
        self.columns.truncate(end);
    }

    fn erase_to_start(&mut self) {
        if self.columns.is_empty() {
            return;
        }
        let cursor = self.cursor.min(self.columns.len() - 1);
        let mut end = cursor + 1;
        if let Some(Column::Glyph(cell)) = self.columns.get(cursor) {
            end = (cursor + cell.width).min(self.columns.len());
        }
        for column in &mut self.columns[..end] {
            *column = Column::Empty;
        }
    }

    fn erase_all(&mut self) {
        self.columns.clear();
    }

    fn render(&self) -> String {
        let length = self
            .columns
            .iter()
            .rposition(|column| match column {
                Column::Glyph(cell) => {
                    cell.character != ' ' || cell.combining.is_some() || cell.style != 0
                }
                Column::Empty | Column::Continuation => false,
            })
            .map_or(0, |index| index + 1);
        let mut rendered = String::new();
        let mut style = 0;
        for column in &self.columns[..length] {
            let cell = match column {
                Column::Empty => {
                    if style != 0 {
                        rendered.push_str("\x1b[0m");
                        style = 0;
                    }
                    rendered.push(' ');
                    continue;
                }
                Column::Glyph(cell) => cell,
                Column::Continuation => continue,
            };
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
            if let Some(combining) = &cell.combining {
                rendered.push_str(combining);
            }
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
        assert_eq!(third.prior, PriorLine::Continue);
        assert_eq!(third.text, "");
        assert_eq!(stream.snapshot().text, "\x1b[31mnew!\x1b[0m");
    }

    #[test]
    fn leaves_unchanged_volatile_state_out_of_later_updates() {
        let mut stream = Stream::default();
        assert_eq!(stream.ingest("value\r").text, "value");
        assert_eq!(stream.ingest("value").text, "");
        assert_eq!(stream.snapshot().text, "value");
        assert_eq!(stream.ingest("\rvalue").text, "");
        assert_eq!(stream.snapshot().text, "");
        assert_eq!(stream.finish().text, "value");
    }

    #[test]
    fn discards_unsupported_controls_without_buffering_their_payloads() {
        let mut stream = Stream::default();
        let output = stream.ingest(&format!("before\x1b]{}\x07\rvisible", "x".repeat(100_000)));
        assert_eq!(output.text, "");
        assert_eq!(stream.snapshot().text, "visible");

        let output = stream.ingest(&format!("\x1b[{}zafter", "1".repeat(100_000)));
        assert_eq!(output.text, "");
        assert_eq!(stream.snapshot().text, "visibleafter");
    }

    #[test]
    fn uses_display_columns_for_combining_marks_and_wide_characters() {
        let mut stream = Stream::default();
        assert_eq!(stream.ingest("\re\u{301}\x08x界\x08\x08!\n").text, "x!\n");
    }

    #[test]
    fn defers_fragmented_volatile_projection_until_snapshot() {
        let mut stream = Stream::default();
        assert_eq!(stream.ingest("\r").text, "");
        for _ in 0..MAX_VISIBLE_CELLS {
            assert_eq!(stream.ingest("x").text, "");
        }
        assert_eq!(stream.snapshot().text.len(), MAX_VISIBLE_CELLS);
    }
}
