//! Carriage-return and backspace projection for one delivered output segment.

#[derive(Default)]
pub(super) struct Stream {
    line: String,
    pending_carriage_return: bool,
    replace_on_write: bool,
}

impl Stream {
    pub(super) fn ingest(&mut self, text: &str) -> String {
        let mut stable = String::new();
        for character in text.chars() {
            self.character(character, &mut stable);
        }
        stable
    }

    pub(super) fn finish(&mut self) -> String {
        self.pending_carriage_return = false;
        self.replace_on_write = false;
        std::mem::take(&mut self.line)
    }

    fn character(&mut self, character: char, stable: &mut String) {
        if self.pending_carriage_return {
            if character == '\r' {
                return;
            }
            if character == '\x08' {
                self.pending_carriage_return = false;
                self.line.pop();
                self.replace_on_write = true;
                return;
            }
            self.pending_carriage_return = false;
            if character == '\n' {
                self.newline("\r\n", stable);
                return;
            }
            self.line.clear();
        }

        match character {
            '\r' => self.pending_carriage_return = true,
            '\n' => self.newline("\n", stable),
            '\x08' => {
                self.line.pop();
            }
            _ => {
                if self.replace_on_write {
                    self.line.clear();
                    self.replace_on_write = false;
                }
                self.line.push(character);
            }
        }
    }

    fn newline(&mut self, delimiter: &str, stable: &mut String) {
        stable.push_str(&self.line);
        stable.push_str(delimiter);
        self.line.clear();
        self.pending_carriage_return = false;
        self.replace_on_write = false;
    }
}
