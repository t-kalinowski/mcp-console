/// Retain the two ends of dynamic control details without losing omission
/// accounting when an unclaimed response is composed and bounded again.
#[derive(Clone, Default)]
pub(crate) struct TextPreview {
    pub(crate) head: String,
    pub(crate) tail: String,
    pub(crate) omitted: u64,
}

impl TextPreview {
    pub(crate) fn read(mut reader: impl std::io::Read, limit: usize) -> std::io::Result<String> {
        let mut preview = Self::default();
        let mut pending = Vec::new();
        let mut buffer = [0; 8192];
        loop {
            let count = match reader.read(&mut buffer) {
                Ok(count) => count,
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error),
            };
            pending.extend_from_slice(&buffer[..count]);
            let complete = if count == 0 {
                pending.len()
            } else {
                complete_utf8_prefix(&pending)
            };
            let text = String::from_utf8_lossy(&pending[..complete]);
            if preview.omitted == 0 {
                preview.head.push_str(&text);
            } else {
                preview.tail.push_str(&text);
            }
            preview.trim(limit);
            pending.drain(..complete);
            if count == 0 {
                return Ok(preview.render());
            }
        }
    }

    pub(crate) fn new(text: String, limit: usize) -> Self {
        let mut control = Self {
            head: text,
            ..Self::default()
        };
        control.trim(limit);
        control
    }

    pub(crate) fn bytes(&self) -> u64 {
        self.head.len() as u64 + self.tail.len() as u64 + self.omitted
    }

    pub(crate) fn marker(&self) -> String {
        if self.omitted == 0 {
            String::new()
        } else {
            format!(
                "[… omitted {} rendered UTF-8 bytes; not retained …]",
                self.omitted
            )
        }
    }

    pub(crate) fn render(&self) -> String {
        format!("{}{}{}", self.head, self.marker(), self.tail)
    }

    pub(crate) fn len(&self) -> usize {
        self.head.len() + self.marker().len() + self.tail.len()
    }

    pub(crate) fn ends_with_newline(&self) -> bool {
        if self.omitted == 0 {
            self.head.ends_with('\n')
        } else {
            self.tail.ends_with('\n')
        }
    }

    pub(crate) fn trim(&mut self, limit: usize) -> bool {
        if self.len() <= limit {
            return true;
        }
        let total = self.bytes();
        let mut omitted = total;
        loop {
            let marker = format!("[… omitted {omitted} rendered UTF-8 bytes; not retained …]");
            if marker.len() > limit {
                return false;
            }
            let budget = limit - marker.len();
            let head = self.head.floor_char_boundary(budget / 2);
            let tail_text = if self.omitted == 0 {
                &self.head
            } else {
                &self.tail
            };
            let tail =
                tail_text.ceil_char_boundary(tail_text.len().saturating_sub(budget - budget / 2));
            let next = total - head as u64 - (tail_text.len() - tail) as u64;
            if next == omitted {
                self.tail = tail_text[tail..].to_owned();
                // Drop the original allocation as well as its visible middle.
                self.head = self.head[..head].to_owned();
                self.omitted = omitted;
                return true;
            }
            omitted = next;
        }
    }
}

pub(crate) fn complete_utf8_prefix(bytes: &[u8]) -> usize {
    let mut offset = 0;
    loop {
        match std::str::from_utf8(&bytes[offset..]) {
            Ok(_) => return bytes.len(),
            Err(error) => match error.error_len() {
                Some(length) => offset += error.valid_up_to() + length,
                None => return offset + error.valid_up_to(),
            },
        }
    }
}
