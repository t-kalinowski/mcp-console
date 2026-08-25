//! A bounded progress-frame compactor, not a terminal emulator.
//!
//! Bare carriage returns replace the current unterminated frame, CRLF remains
//! a newline, backspace removes one Unicode scalar, and ANSI erase-line clears
//! the frame. Other text and controls are preserved without interpretation.

const MAX_CONTROL_BYTES: usize = 64;
pub(super) const MAX_COMPACT_LINE_BYTES: usize = 16 * 1024;

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

pub(super) struct ActiveOmission {
    pub(super) sequence: u64,
    pub(super) text_bytes: usize,
    pub(super) events: usize,
}

#[derive(Default)]
pub(super) struct Stream {
    line: String,
    parser: Parser,
    pending_carriage_return: bool,
    volatile: bool,
    replace_on_write: bool,
    passthrough: bool,
    active_events: Vec<u64>,
    active_omissions: Vec<ActiveOmission>,
    last_observation: Option<u64>,
}

#[derive(Default)]
enum Parser {
    #[default]
    Ground,
    Escape(String),
    Csi(String),
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
        let mut collector = Collector::new(self);
        for character in text.chars() {
            collector.character(character);
        }
        collector.finish_update()
    }

    pub(super) fn finish(&mut self) -> Update {
        let mut collector = Collector::new(self);
        if collector.stream.pending_carriage_return {
            collector.stream.pending_carriage_return = false;
            collector.carriage_return();
        }
        collector.flush_parser();

        let mut text = collector.stable;
        let prior = if collector.stream.volatile {
            text.push_str(&collector.stream.line);
            PriorLine::Replace
        } else {
            text.push_str(&collector.ordinary_active);
            PriorLine::Finalized
        };
        collector.stream.reset_all();
        Update {
            prior,
            text,
            active_suffix: None,
        }
    }

    pub(super) fn pass_through_line(&mut self) {
        self.passthrough = true;
    }

    pub(super) fn take_active_events(&mut self) -> Vec<u64> {
        std::mem::take(&mut self.active_events)
    }

    pub(super) fn push_active_event(&mut self, sequence: u64) {
        self.active_events.push(sequence);
    }

    pub(super) fn take_active_omissions(&mut self) -> Vec<ActiveOmission> {
        std::mem::take(&mut self.active_omissions)
    }

    pub(super) fn push_active_omission(&mut self, sequence: u64, text_bytes: usize, events: usize) {
        if let Some(omission) = self
            .active_omissions
            .iter_mut()
            .find(|omission| omission.sequence == sequence)
        {
            omission.text_bytes = omission.text_bytes.saturating_add(text_bytes);
            omission.events = omission.events.saturating_add(events);
        } else {
            self.active_omissions.push(ActiveOmission {
                sequence,
                text_bytes,
                events,
            });
        }
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
        self.replace_on_write = false;
        self.passthrough = false;
    }

    fn reset_all(&mut self) {
        self.reset_line();
        self.parser = Parser::Ground;
        self.last_observation = None;
    }
}

impl<'a> Collector<'a> {
    fn new(stream: &'a mut Stream) -> Self {
        Self {
            stream,
            prior: PriorLine::Continue,
            prior_open: true,
            stable: String::new(),
            ordinary_active: String::new(),
        }
    }

    fn character(&mut self, character: char) {
        let parser = std::mem::take(&mut self.stream.parser);
        match parser {
            Parser::Ground => self.ground(character),
            Parser::Escape(sequence) | Parser::Csi(sequence)
                if matches!(character, '\r' | '\n') =>
            {
                self.write_literal(&sequence);
                self.ground(character);
            }
            Parser::Escape(mut sequence) => {
                sequence.push(character);
                if character == '[' {
                    self.stream.parser = Parser::Csi(sequence);
                } else {
                    self.write_literal(&sequence);
                }
            }
            Parser::Csi(mut sequence) => {
                sequence.push(character);
                if sequence.len() > MAX_CONTROL_BYTES {
                    self.write_literal(&sequence);
                } else if is_csi_final(character) {
                    if character == 'K' {
                        self.erase_line(&sequence);
                    } else {
                        self.write_literal(&sequence);
                    }
                } else {
                    self.stream.parser = Parser::Csi(sequence);
                }
            }
        }
    }

    fn ground(&mut self, character: char) {
        if self.stream.passthrough {
            if character == '\n' {
                self.newline("\n");
            } else {
                self.ordinary_active.push(character);
            }
            return;
        }

        if self.stream.pending_carriage_return {
            self.stream.pending_carriage_return = false;
            if character == '\n' {
                self.newline("\r\n");
                return;
            }
            self.carriage_return();
        }

        match character {
            '\r' => {
                self.stream.pending_carriage_return = true;
                if !self.stream.passthrough {
                    self.begin_volatile();
                }
            }
            '\n' => self.newline("\n"),
            '\x08' => self.backspace(),
            '\x1b' => {
                self.stream.parser = Parser::Escape("\x1b".to_string());
            }
            _ => self.write(character),
        }
    }

    fn write(&mut self, character: char) {
        if self.stream.passthrough {
            self.ordinary_active.push(character);
            return;
        }
        if self.stream.volatile && self.stream.replace_on_write {
            self.stream.line.clear();
            self.stream.replace_on_write = false;
        }
        self.stream.line.push(character);
        if self.stream.volatile {
            if self.stream.line.len() > MAX_COMPACT_LINE_BYTES {
                self.abandon_compaction();
            }
        } else {
            self.ordinary_active.push(character);
            if self.stream.line.len() > MAX_COMPACT_LINE_BYTES {
                self.abandon_compaction();
            }
        }
    }

    fn write_literal(&mut self, text: &str) {
        for character in text.chars() {
            self.write(character);
        }
    }

    fn backspace(&mut self) {
        if self.stream.passthrough {
            self.ordinary_active.push('\x08');
            return;
        }
        self.begin_volatile();
        if self.stream.replace_on_write {
            self.stream.line.clear();
            self.stream.replace_on_write = false;
        }
        self.stream.line.pop();
    }

    fn erase_line(&mut self, sequence: &str) {
        if self.stream.passthrough {
            self.ordinary_active.push_str(sequence);
            return;
        }
        self.begin_volatile();
        self.stream.line.clear();
        self.stream.replace_on_write = false;
    }

    fn carriage_return(&mut self) {
        if self.stream.passthrough {
            self.ordinary_active.push('\r');
            return;
        }
        self.begin_volatile();
        self.stream.replace_on_write = true;
    }

    fn begin_volatile(&mut self) {
        if self.stream.volatile {
            return;
        }
        self.stream.volatile = true;
        self.ordinary_active.clear();
        if self.prior_open {
            self.prior = PriorLine::Replace;
        }
    }

    fn abandon_compaction(&mut self) {
        if self.stream.volatile {
            if self.prior_open {
                self.prior = PriorLine::Replace;
            }
            self.stable.push_str(&self.stream.line);
        } else if self.prior_open {
            self.prior = PriorLine::Finalized;
        }
        self.stream.line.clear();
        self.stream.volatile = false;
        self.stream.replace_on_write = false;
        self.stream.passthrough = true;
    }

    fn newline(&mut self, delimiter: &str) {
        if self.stream.passthrough {
            self.stable.push_str(&self.ordinary_active);
            self.stable.push_str(delimiter);
            if self.prior_open && self.prior == PriorLine::Continue {
                self.prior = PriorLine::Finalized;
            }
        } else if self.stream.volatile {
            if self.prior_open {
                self.prior = PriorLine::Replace;
            }
            self.stable.push_str(&self.stream.line);
            self.stable.push_str(delimiter);
        } else {
            if self.prior_open {
                self.prior = PriorLine::Finalized;
            }
            self.stable.push_str(&self.ordinary_active);
            self.stable.push_str(delimiter);
        }
        self.stream.reset_line();
        self.prior_open = false;
        self.ordinary_active.clear();
    }

    fn flush_parser(&mut self) {
        match std::mem::take(&mut self.stream.parser) {
            Parser::Ground => {}
            Parser::Escape(sequence) | Parser::Csi(sequence) => self.write_literal(&sequence),
        }
    }

    fn finish_update(mut self) -> Update {
        let active = if self.stream.volatile {
            String::new()
        } else {
            std::mem::take(&mut self.ordinary_active)
        };
        let active_suffix =
            (!self.stream.passthrough && !active.is_empty()).then_some(self.stable.len());
        self.stable.push_str(&active);
        Update {
            prior: self.prior,
            text: self.stable,
            active_suffix,
        }
    }
}

fn is_csi_final(character: char) -> bool {
    ('@'..='~').contains(&character)
}
