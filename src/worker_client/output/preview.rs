//! Bounded ordered text projection, independent of raw-file retention.

use super::{Content, utf8_prefix_length};

/// Complete rendered tool-result text, including all Console notices.
pub(crate) const TEXT_BYTES: usize = 8 * 1024;
const COLLECT_BYTES: usize = 2 * TEXT_BYTES;
const CONTROL_BYTES: usize = TEXT_BYTES / 2;
pub(super) const IMAGE_BYTES: usize = 8 * 1024 * 1024;
const IMAGE_METADATA_BYTES: usize = 64 * 1024;
const IMAGE_EVENTS: usize = 4096;

mod source;
pub(super) use source::Source;
use source::Summary;

#[derive(Clone)]
pub(super) enum Part {
    Text(String),
    Gap(Gap),
    Notice(Control),
    Information(String),
    Image(Content),
    ImageGap,
    Source(Source),
    Summary(Summary),
}

#[derive(Clone, Copy, Default)]
pub(super) struct Gap {
    bytes: u64,
    notices: u64,
}

#[derive(Clone, Default)]
pub(super) struct Preview {
    pub(super) parts: Vec<Part>,
    image_bytes: usize,
    image_metadata_bytes: usize,
    image_events: usize,
    omitted_images: u64,
    omitted_image_bytes: u64,
    recorded_omitted_images: u64,
    omitted_controls: u64,
    omitted_control_bytes: u64,
}

impl Preview {
    pub(super) fn text(&mut self, text: &str) {
        if text.is_empty() {
            return;
        }
        // Never copy an oversized publication in full into retained state.
        if text.len() > COLLECT_BYTES {
            let head = utf8_prefix_length(text, TEXT_BYTES);
            let tail = suffix_start(text, TEXT_BYTES);
            self.push_text(&text[..head]);
            self.parts.push(Part::Gap(Gap {
                bytes: (tail - head) as u64,
                notices: 0,
            }));
            self.push_text(&text[tail..]);
        } else {
            self.push_text(text);
        }
        self.trim(COLLECT_BYTES);
    }

    fn push_text(&mut self, text: &str) {
        if text.is_empty() {
            return;
        }
        self.parts.push(Part::Text(text.to_owned()));
    }

    fn omitted(&mut self, gap: Gap) {
        if gap.bytes != 0 {
            if let Some(Part::Gap(previous)) = self.parts.last_mut() {
                previous.bytes += gap.bytes;
                previous.notices += gap.notices;
            } else {
                self.parts.push(Part::Gap(gap));
            }
        }
    }

    pub(super) fn notice(&mut self, text: String) {
        self.control(Control::new(text));
    }

    fn control(&mut self, mut control: Control) {
        // A sealed interval can begin with a notice before it is composed with
        // earlier output. Apply its line boundary at that composition point too.
        if !self.is_empty() && !self.ends_with_newline() && !control.head.starts_with('\n') {
            control.head.insert(0, '\n');
        }
        self.parts.push(Part::Notice(control));
        self.trim_controls(COLLECT_BYTES, TEXT_BYTES);
    }

    fn trim_controls(&mut self, limit: usize, each: usize) {
        // Repeated input reports may accumulate before a cut. Preserve the latest
        // active report and summarize older reports when their storage is exhausted.
        let mut remaining = limit;
        for part in self.parts.iter_mut().rev() {
            if let Part::Notice(control) = part {
                if !control.trim(remaining.min(each)) {
                    self.omitted_controls += 1;
                    self.omitted_control_bytes += control.bytes();
                    control.head.clear();
                    control.tail.clear();
                    control.omitted = 0;
                }
                remaining -= control.len();
            }
        }
        self.parts
            .retain(|part| !matches!(part, Part::Notice(control) if control.bytes() == 0));
    }

    pub(super) fn information(&mut self, text: String) {
        if text.len() > COLLECT_BYTES {
            self.omitted(Gap {
                bytes: text.len() as u64,
                notices: text.len() as u64,
            });
        } else {
            self.parts.push(Part::Information(text));
            self.trim(COLLECT_BYTES);
        }
    }

    pub(super) fn admits_image(&self, data: &str, mime_type: &str) -> bool {
        self.image_events < IMAGE_EVENTS
            && data.len() <= IMAGE_BYTES.saturating_sub(self.image_bytes)
            && mime_type.len() <= IMAGE_METADATA_BYTES.saturating_sub(self.image_metadata_bytes)
    }

    pub(super) fn omit_image(&mut self, bytes: usize) {
        if self.omitted_images == 0 {
            self.parts.push(Part::ImageGap);
        }
        self.omitted_images = self.omitted_images.saturating_add(1);
        self.omitted_image_bytes = self.omitted_image_bytes.saturating_add(bytes as u64);
    }

    pub(super) fn image(&mut self, content: Content) {
        let Content::Image {
            data,
            mime_type,
            artifact,
        } = &content
        else {
            unreachable!("only image content enters the independent image budget")
        };
        if !self.admits_image(data, mime_type) {
            self.recorded_omitted_images += u64::from(artifact.is_some());
            self.omit_image(data.len());
            return;
        }
        self.image_bytes += data.len();
        self.image_metadata_bytes += mime_type.len();
        self.image_events += 1;
        self.parts.push(Part::Image(content));
    }

    pub(super) fn source(&mut self, source: Source) {
        let has_text = self
            .parts
            .iter()
            .rev()
            .take_while(|part| !matches!(part, Part::Source(_)))
            .any(|part| matches!(part, Part::Text(_) | Part::Gap(_) | Part::Information(_)));
        // Publish empty files, but retain no receipt when there is no rendered
        // text to attribute. Raw terminal edits can also render no text.
        if !has_text {
            if let Some(file) = source.file {
                file.publish();
            }
            return;
        }
        self.parts.push(Part::Source(source));
        self.summarize_sources();
    }

    pub(super) fn extend(&mut self, other: Self) {
        for part in other.parts {
            match part {
                Part::Text(text) => self.text(&text),
                Part::Gap(gap) => self.omitted(gap),
                Part::Notice(control) => self.control(control),
                Part::Information(text) => self.information(text),
                Part::ImageGap => {
                    if self.omitted_images == 0 {
                        self.parts.push(Part::ImageGap);
                    }
                    self.omitted_images += other.omitted_images;
                    self.omitted_image_bytes += other.omitted_image_bytes;
                    self.recorded_omitted_images += other.recorded_omitted_images;
                }
                Part::Image(image) => self.image(image),
                Part::Source(source) => self.source(source),
                Part::Summary(summary) => self.parts.push(Part::Summary(summary)),
            }
        }
        self.omitted_controls += other.omitted_controls;
        self.omitted_control_bytes += other.omitted_control_bytes;
        self.summarize_sources();
    }

    pub(super) fn is_empty(&self) -> bool {
        self.parts
            .iter()
            .all(|part| matches!(part, Part::Source(_)))
            && self.omitted_images == 0
            && self.omitted_controls == 0
    }

    pub(super) fn ends_with_newline(&self) -> bool {
        match self.last_visible() {
            Some(Part::Text(text) | Part::Information(text)) => text.ends_with('\n'),
            Some(Part::Notice(control)) => control.ends_with_newline(),
            Some(Part::Summary(_)) => true,
            _ => false,
        }
    }

    pub(super) fn last_visible(&self) -> Option<&Part> {
        self.parts
            .iter()
            .rev()
            .find(|part| !matches!(part, Part::Source(_)))
    }

    pub(super) fn starts_with_text(&self) -> bool {
        matches!(
            self.parts
                .iter()
                .find(|part| !matches!(part, Part::Source(_))),
            Some(Part::Text(_) | Part::Notice(_) | Part::Information(_) | Part::Gap(_))
        )
    }

    fn trim(&mut self, limit: usize) {
        let total: u64 = self
            .parts
            .iter()
            .map(|part| match part {
                Part::Text(text) | Part::Information(text) => text.len() as u64,
                Part::Gap(gap) => gap.bytes,
                Part::Summary(summary) => summary.gap.bytes,
                _ => 0,
            })
            .sum();
        if total <= limit as u64 {
            return;
        }
        let head_end = (limit / 2) as u64;
        let tail_start = total.saturating_sub((limit - limit / 2) as u64);
        let mut position = 0u64;
        let mut parts = Vec::new();
        for part in std::mem::take(&mut self.parts) {
            match part {
                Part::Text(text) => {
                    let head =
                        utf8_prefix_length(&text, head_end.saturating_sub(position) as usize);
                    let tail = if position + text.len() as u64 > tail_start {
                        suffix_start(&text, (position + text.len() as u64 - tail_start) as usize)
                    } else {
                        text.len()
                    }
                    .max(head);
                    if head != 0 {
                        parts.push(Part::Text(text[..head].to_owned()));
                    }
                    if tail > head {
                        parts.push(Part::Gap(Gap {
                            bytes: (tail - head) as u64,
                            notices: 0,
                        }));
                    }
                    if tail < text.len() {
                        parts.push(Part::Text(text[tail..].to_owned()));
                    }
                    position += text.len() as u64;
                }
                Part::Gap(gap) => {
                    position += gap.bytes;
                    parts.push(Part::Gap(gap));
                }
                Part::Summary(summary) => {
                    position += summary.gap.bytes;
                    parts.push(Part::Summary(summary));
                }
                Part::Information(text) => {
                    let end = position + text.len() as u64;
                    if end <= head_end || position >= tail_start {
                        parts.push(Part::Information(text));
                    } else {
                        parts.push(Part::Gap(Gap {
                            bytes: text.len() as u64,
                            notices: text.len() as u64,
                        }));
                    }
                    position = end;
                }
                other => parts.push(other),
            }
        }
        for part in parts {
            match (self.parts.last_mut(), part) {
                (Some(Part::Gap(previous)), Part::Gap(gap)) => {
                    previous.bytes += gap.bytes;
                    previous.notices += gap.notices;
                }
                (Some(Part::Text(previous)), Part::Text(text)) => previous.push_str(&text),
                (_, part) => self.parts.push(part),
            }
        }
        self.summarize_sources();
    }

    /// Fully omitted intervals no longer need live per-file receipts. Account
    /// them once, then keep one summary at the first such omission. Text-bearing
    /// intervals retain their own receipts so later trims can update their counts.
    fn summarize_sources(&mut self) {
        let mut start = 0;
        for end in 0..self.parts.len() {
            let Part::Source(source) = &self.parts[end] else {
                continue;
            };
            let parts = &self.parts[start..end];
            if !parts
                .iter()
                .any(|part| matches!(part, Part::Text(_) | Part::Information(_)))
            {
                let mut gap = Gap::default();
                for part in parts {
                    if let Part::Gap(omitted) = part {
                        gap.bytes += omitted.bytes;
                        gap.notices += omitted.notices;
                    }
                }
                let mut summary = Summary::new(source, gap);
                for part in &mut self.parts[start..=end] {
                    if matches!(part, Part::Gap(_) | Part::Source(_)) {
                        *part = Part::Summary(std::mem::take(&mut summary));
                    }
                }
            }
            start = end + 1;
        }
        let mut combined = Summary::default();
        let mut first = None;
        for (index, part) in self.parts.iter_mut().enumerate() {
            if let Part::Summary(summary) = part {
                if summary.gap.bytes != 0 {
                    first.get_or_insert(index);
                }
                combined.extend(std::mem::take(summary));
            }
        }
        if let Some(first) = first {
            self.parts[first] = Part::Summary(combined);
        }
        self.parts
            .retain(|part| !matches!(part, Part::Summary(summary) if summary.gap.bytes == 0));
    }

    /// Reserve notices first, then divide ordinary text between its head and tail.
    pub(super) fn render(&mut self) -> Vec<Content> {
        let mut allowance = TEXT_BYTES;
        loop {
            self.trim(allowance);
            let mut bytes = text_bytes(&self.project(false));
            if bytes > TEXT_BYTES {
                self.trim_controls(CONTROL_BYTES, TEXT_BYTES / 8);
                bytes = text_bytes(&self.project(false));
            }
            if bytes <= TEXT_BYTES {
                return self.project(true);
            }
            assert!(
                allowance > 0,
                "bounded response notices exceed the complete result budget"
            );
            // Reserve notices in small blocks so incidental path and count widths
            // do not continually move the visible text boundaries.
            allowance = allowance.saturating_sub(bytes - TEXT_BYTES) / 128 * 128;
        }
    }

    fn project(&self, account: bool) -> Vec<Content> {
        let mut content = Vec::new();
        if self.omitted_controls != 0 {
            append_text(
                &mut content,
                &format!(
                    "[control preview: omitted {} earlier notices ({} rendered UTF-8 bytes); not retained]\n",
                    self.omitted_controls, self.omitted_control_bytes,
                ),
            );
        }
        let mut image_marker = true;
        let mut start = 0;
        for end in 0..=self.parts.len() {
            let source = match self.parts.get(end) {
                Some(Part::Source(source)) => source.clone(),
                None => Source::default(),
                _ => continue,
            };
            let parts = &self.parts[start..end];
            let omitted: u64 = parts
                .iter()
                .map(|part| {
                    if let Part::Gap(gap) = part {
                        gap.bytes
                    } else {
                        0
                    }
                })
                .sum();
            let notices: u64 = parts
                .iter()
                .map(|part| match part {
                    Part::Gap(gap) => gap.notices,
                    _ => 0,
                })
                .sum();
            let mut marker = (omitted != 0).then(|| source.notice(omitted, notices));
            for part in parts {
                match part {
                    Part::Text(text) | Part::Information(text) => append_text(&mut content, text),
                    Part::Notice(control) => append_text(&mut content, &control.render()),
                    Part::Gap(_) => {
                        if let Some(marker) = marker.take() {
                            append_text(&mut content, &marker);
                        }
                    }
                    Part::Summary(summary) => append_text(&mut content, &summary.notice()),
                    Part::Image(image) => content.push(image.clone()),
                    Part::ImageGap => {
                        if image_marker {
                            append_text(
                                &mut content,
                                &format!(
                                    "\n[image limit: omitted {} images ({} encoded bytes); {} already recorded, {} not retained]\n",
                                    self.omitted_images,
                                    self.omitted_image_bytes,
                                    self.recorded_omitted_images,
                                    self.omitted_images - self.recorded_omitted_images
                                ),
                            );
                            image_marker = false;
                        }
                    }
                    Part::Source(_) => unreachable!(),
                }
            }
            if account && let Some(file) = &source.file {
                file.note_inline_omission(omitted);
            }
            start = end + 1;
        }
        if account {
            for part in &self.parts {
                if let Part::Source(Source {
                    file: Some(file), ..
                }) = part
                {
                    file.publish();
                }
            }
        }
        content
    }
}

fn append_text(content: &mut Vec<Content>, text: &str) {
    if text.is_empty() {
        return;
    }
    if let Some(Content::Text(previous)) = content.last_mut() {
        previous.push_str(text);
    } else {
        content.push(Content::Text(text.to_owned()));
    }
}

pub(super) fn suffix_start(text: &str, limit: usize) -> usize {
    let mut start = text.len().saturating_sub(limit);
    while !text.is_char_boundary(start) {
        start += 1;
    }
    start
}

/// Retain the two ends of dynamic control details without losing omission
/// accounting when an unclaimed response is composed and bounded again.
#[derive(Clone, Default)]
pub(super) struct Control {
    head: String,
    tail: String,
    omitted: u64,
}

impl Control {
    fn new(text: String) -> Self {
        let mut control = Self {
            head: text,
            ..Self::default()
        };
        control.trim(TEXT_BYTES);
        control
    }

    fn bytes(&self) -> u64 {
        self.head.len() as u64 + self.tail.len() as u64 + self.omitted
    }

    fn marker(&self) -> String {
        if self.omitted == 0 {
            String::new()
        } else {
            format!(
                "[… omitted {} rendered UTF-8 bytes; not retained …]",
                self.omitted
            )
        }
    }

    fn render(&self) -> String {
        format!("{}{}{}", self.head, self.marker(), self.tail)
    }

    fn len(&self) -> usize {
        self.head.len() + self.marker().len() + self.tail.len()
    }

    fn ends_with_newline(&self) -> bool {
        if self.omitted == 0 {
            self.head.ends_with('\n')
        } else {
            self.tail.ends_with('\n')
        }
    }

    fn trim(&mut self, limit: usize) -> bool {
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
            let head = utf8_prefix_length(&self.head, budget / 2);
            let tail_text = if self.omitted == 0 {
                &self.head
            } else {
                &self.tail
            };
            let tail = suffix_start(tail_text, budget - budget / 2);
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

fn text_bytes(content: &[Content]) -> usize {
    content
        .iter()
        .map(|part| match part {
            Content::Text(text) => text.len(),
            Content::Image { .. } => 0,
        })
        .sum()
}
