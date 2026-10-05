//! Bounded carriage-return and backspace projection for one response interval.

use super::preview::{Preview, TEXT_BYTES, suffix_start};
use super::utf8_prefix_length;

#[derive(Default)]
pub(super) struct Stream {
    head: String,
    tail: String,
    tail_start: usize,
    omitted: u64,
    head_closed: bool,
    pending_carriage_return: bool,
    replace_on_write: bool,
}

impl Stream {
    pub(super) fn ingest(&mut self, text: &str, output: &mut Preview) {
        for run in text.split_inclusive(['\r', '\n', '\x08']) {
            let (plain, control) = match run.as_bytes().last() {
                Some(b'\r' | b'\n' | 8) => (&run[..run.len() - 1], run.chars().next_back()),
                _ => (run, None),
            };
            if !plain.is_empty() {
                if self.pending_carriage_return || self.replace_on_write {
                    self.clear();
                }
                self.append(plain);
            }
            match control {
                Some('\r') => self.pending_carriage_return = true,
                Some('\n') => {
                    let delimiter = if self.pending_carriage_return {
                        "\r\n"
                    } else {
                        "\n"
                    };
                    self.finish(output);
                    output.text(delimiter);
                }
                Some('\x08') => {
                    if self.pending_carriage_return {
                        self.pending_carriage_return = false;
                        self.replace_on_write = true;
                    }
                    // Backspace edits the retained suffix. It cannot reconstruct
                    // an already omitted middle after erasing that entire suffix;
                    // the gap remains until a carriage return replaces the frame.
                    if self.tail.len() > self.tail_start {
                        self.tail.pop();
                    } else if self.omitted == 0 {
                        self.head.pop();
                    }
                }
                _ => {}
            }
        }
    }

    fn append(&mut self, text: &str) {
        let head = if self.head_closed {
            0
        } else {
            utf8_prefix_length(text, TEXT_BYTES.saturating_sub(self.head.len()))
        };
        self.head.push_str(&text[..head]);
        let text = &text[head..];
        self.head_closed |= !text.is_empty();
        if text.is_empty() {
            return;
        }
        if text.len() >= TEXT_BYTES {
            let start = suffix_start(text, TEXT_BYTES);
            self.omitted += (self.tail.len() - self.tail_start + start) as u64;
            self.tail.clear();
            self.tail_start = 0;
            self.tail.push_str(&text[start..]);
        } else {
            // Advance through the retained suffix between bounded compactions.
            // Reserve only this fixed ceiling, including the discarded prefix.
            if self.tail.len() + text.len() > 2 * TEXT_BYTES {
                self.tail.drain(..self.tail_start);
                self.tail_start = 0;
            }
            if self.tail.capacity() < self.tail.len() + text.len() {
                self.tail.reserve_exact(2 * TEXT_BYTES - self.tail.len());
            }
            self.tail.push_str(text);
            let start = suffix_start(&self.tail[self.tail_start..], TEXT_BYTES);
            self.omitted += start as u64;
            self.tail_start += start;
        }
    }

    fn clear(&mut self) {
        self.head.clear();
        self.tail.clear();
        self.tail_start = 0;
        self.omitted = 0;
        self.head_closed = false;
        self.pending_carriage_return = false;
        self.replace_on_write = false;
    }

    pub(super) fn finish(&mut self, output: &mut Preview) {
        output.text(&self.head);
        output.gap(self.omitted);
        output.text(&self.tail[self.tail_start..]);
        self.clear();
    }
}
