//! Incremental report projection. Result text is spooled by observed interval,
//! so later polls join their cells without retaining response history in memory.
use super::event::{Envelope, Event, Outcome, RecordedContent};
use super::{Interval, Owner};
use std::collections::{BTreeMap, BTreeSet};
use std::io::{Read as _, Seek as _, SeekFrom, Write as _};
use std::sync::{Arc, Mutex};

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum SectionId {
    Startup,
    Cell(u64),
    Post(u64),
    Submission(u64),
    Notices(u64),
}
impl SectionId {
    fn owner(owner: Owner) -> Self {
        match owner.interval {
            Interval::Startup => Self::Startup,
            Interval::Cell => Self::Cell(owner.cell_id.expect("cell identity")),
            Interval::Post => Self::Post(owner.cell_id.expect("post identity")),
        }
    }
    fn heading(self) -> String {
        match self {
            Self::Startup => "Startup output".into(),
            Self::Cell(id) => format!("Cell {id:03}"),
            Self::Post(id) => format!("Between cells (after {id:03})"),
            Self::Submission(_) => "Unexecuted submission".into(),
            Self::Notices(_) => "Response notices".into(),
        }
    }
}
struct Section {
    spool: Arc<Mutex<std::fs::File>>,
    ranges: Vec<(u64, u64)>,
    sequence: u64,
    completion: Option<bool>,
    length: u64,
}
impl Section {
    fn new(sequence: u64, spool: Arc<Mutex<std::fs::File>>) -> Self {
        Self {
            spool,
            ranges: Vec::new(),
            sequence,
            completion: None,
            length: 0,
        }
    }
    fn append(&mut self, bytes: &[u8]) -> Result<(), String> {
        let mut spool = self
            .spool
            .lock()
            .map_err(|_| "report spool lock poisoned")?;
        let offset = spool.seek(SeekFrom::End(0)).map_err(|e| e.to_string())?;
        spool.write_all(bytes).map_err(|e| e.to_string())?;
        self.length += bytes.len() as u64;
        if let Some((previous, length)) = self.ranges.last_mut()
            && *previous + *length == offset
        {
            *length += bytes.len() as u64;
        } else {
            self.ranges.push((offset, bytes.len() as u64));
        }
        Ok(())
    }
    fn fence(&mut self, language: &str, text: &str) -> Result<(), String> {
        let mut fragment = String::new();
        super::markdown::push_fence(&mut fragment, language, text);
        self.append(fragment.as_bytes())
    }
}
struct Pending {
    section: Option<Section>,
    target: Option<(u64, SectionId)>,
    admitted: bool,
    settled: bool,
    result: bool,
}
struct PublishedSection {
    start: u64,
    content_end: u64,
    length: u64,
}
struct Published {
    file: std::fs::File,
    sections: BTreeMap<(u64, SectionId), PublishedSection>,
    end: u64,
}
pub(super) struct Reports {
    spool: Arc<Mutex<std::fs::File>>,
    pending: BTreeMap<u64, Pending>,
    sections: BTreeMap<(u64, SectionId), Section>,
    order: BTreeSet<(u64, u64, SectionId)>,
    dirty: BTreeSet<(u64, u64, SectionId)>,
    published: Option<(u64, Published)>,
    current: Option<u64>,
}
impl Reports {
    pub(super) fn new() -> Result<Self, String> {
        // Keep one anonymous handle throughout projection. Never reopen a
        // temporary path that submitted code might replace with a host symlink.
        Ok(Self {
            spool: Arc::new(Mutex::new(tempfile::tempfile().map_err(|e| e.to_string())?)),
            pending: BTreeMap::new(),
            sections: BTreeMap::new(),
            order: BTreeSet::new(),
            dirty: BTreeSet::new(),
            published: None,
            current: None,
        })
    }

    fn section(&mut self, session: u64, id: SectionId, sequence: u64) -> &mut Section {
        if !self.sections.contains_key(&(session, id)) {
            self.insert_section(session, id, Section::new(sequence, self.spool.clone()));
        }
        let section = self.sections.get_mut(&(session, id)).unwrap();
        self.dirty.insert((session, section.sequence, id));
        section
    }
    fn insert_section(&mut self, session: u64, id: SectionId, section: Section) {
        self.order.insert((session, section.sequence, id));
        self.dirty.insert((session, section.sequence, id));
        self.sections.insert((session, id), section);
    }
    fn remove_section(&mut self, session: u64, id: SectionId) -> Option<Section> {
        let section = self.sections.remove(&(session, id))?;
        self.order.remove(&(session, section.sequence, id));
        self.dirty.insert((session, section.sequence, id));
        Some(section)
    }
    pub(super) fn observe(&mut self, envelope: &Envelope<'_>) -> Result<Vec<u64>, String> {
        let sequence = envelope.sequence;
        let mut changed = Vec::new();
        match &envelope.event {
            Event::WorkerStarted { session_id, .. } => {
                self.current = Some(*session_id);
                self.section(*session_id, SectionId::Startup, sequence);
            }
            Event::WorkerEnded { .. } => self.current = None,
            Event::ToolCall {
                call_id, request, ..
            } => {
                let mut section = None;
                if let Some(arguments) = &request.arguments {
                    for language in ["r", "python", "sql"] {
                        if let Some(source) =
                            arguments.get(language).and_then(serde_json::Value::as_str)
                        {
                            if section.is_none() {
                                section = Some(Section::new(sequence, self.spool.clone()));
                            }
                            section.as_mut().unwrap().fence(language, source)?;
                        }
                    }
                }
                if let Some(section) = section {
                    self.pending.insert(
                        *call_id,
                        Pending {
                            section: Some(section),
                            target: None,
                            admitted: false,
                            settled: false,
                            result: false,
                        },
                    );
                }
            }
            Event::SourceAdmitted { call_id } => {
                if let Some(pending) = self.pending.get_mut(call_id) {
                    pending.admitted = true;
                }
            }
            Event::SourceAssociated {
                call_id,
                session_id,
            } => {
                if let Some(pending) = self.pending.get_mut(call_id) {
                    let key = (*session_id, SectionId::Submission(*call_id));
                    pending.target = Some(key);
                    if let Some(section) = pending.section.take() {
                        self.insert_section(key.0, key.1, section);
                    }
                    changed.push(*session_id);
                }
            }
            Event::CellStarted { call_id, owner } => {
                let key = (owner.session_id, SectionId::owner(*owner));
                if let Some(pending) = self.pending.get_mut(call_id)
                    && let Some(previous) = pending.target.replace(key)
                    && let Some(mut section) = self.remove_section(previous.0, previous.1)
                {
                    section.sequence = sequence;
                    self.insert_section(key.0, key.1, section);
                }
                self.section(key.0, key.1, sequence);
                changed.push(owner.session_id);
            }
            Event::CellEnded {
                owner,
                completion_confirmed,
                ..
            } => {
                self.section(owner.session_id, SectionId::owner(*owner), sequence)
                    .completion = Some(*completion_confirmed);
                changed.push(owner.session_id);
                self.section(
                    owner.session_id,
                    SectionId::Post(owner.cell_id.expect("cell identity")),
                    sequence,
                );
            }
            Event::SourceFinished { call_id } => {
                if let Some(pending) = self.pending.get_mut(call_id) {
                    pending.settled = true;
                    if pending.result {
                        self.pending.remove(call_id);
                    }
                }
            }
            Event::ToolResult {
                call_id,
                parts,
                outcome,
            } => {
                let target = self.pending.get(call_id).and_then(|p| p.target);
                match outcome {
                    Outcome::Error { error } => {
                        if let Some((session, id)) = target {
                            self.section(session, id, sequence)
                                .fence("text", &error.message)?;
                            changed.push(session);
                        } else if let Some(section) = self
                            .pending
                            .get_mut(call_id)
                            .and_then(|p| p.section.as_mut())
                        {
                            section.fence("text", &error.message)?;
                        }
                    }
                    Outcome::Result { result } => {
                        for (index, block) in result.content.iter().enumerate() {
                            if matches!(block, RecordedContent::Text(text) if text.meta.as_ref().is_some_and(|meta| meta.get(super::BUNDLE_META) == Some(&serde_json::Value::Bool(true))))
                            {
                                continue;
                            }
                            // The output tape owns ranges, including images. Status text
                            // without an owner belongs to the source submission, if any.
                            let ranges: Vec<_> =
                                parts.iter().filter(|part| part.block == index).collect();
                            if ranges.is_empty()
                                && let RecordedContent::Text(text) = block
                            {
                                if let Some((session, id)) = target {
                                    self.section(session, id, sequence)
                                        .fence("text", &text.text)?;
                                    changed.push(session);
                                } else if let Some(section) = self
                                    .pending
                                    .get_mut(call_id)
                                    .and_then(|p| p.section.as_mut())
                                {
                                    section.fence("text", &text.text)?;
                                }
                            }
                            for part in ranges {
                                let key = part
                                    .owner
                                    .map(|owner| (owner.session_id, SectionId::owner(owner)))
                                    .or(target)
                                    .or_else(|| {
                                        self.current
                                            .filter(|_| result.is_error == Some(true))
                                            .map(|id| (id, SectionId::Notices(*call_id)))
                                    });
                                let Some((session, id)) = key else {
                                    continue;
                                };
                                let section = self.section(session, id, sequence);
                                match block {
                                    RecordedContent::Text(text) => section.fence(
                                        "text",
                                        text.text
                                            .get(part.start..part.end)
                                            .ok_or("report text range unavailable")?,
                                    )?,
                                    RecordedContent::Image {
                                        artifact_id,
                                        retained,
                                        mime_type,
                                        ..
                                    } => {
                                        if *retained != Some(false)
                                            && let Some(id) = artifact_id
                                        {
                                            section.append(
                                                format!(
                                                    "![Recorded image](<images/{}>)\n\n",
                                                    super::bundle_image_name(*id, mime_type)
                                                )
                                                .as_bytes(),
                                            )?;
                                        } else {
                                            section.fence("text", "[Recorded image unavailable: capture did not retain this image.]")?;
                                        }
                                    }
                                    RecordedContent::Other(_) => {}
                                }
                                changed.push(session);
                            }
                        }
                    }
                }
                if let Some(pending) = self.pending.get_mut(call_id) {
                    pending.result = true;
                    if pending.settled || !pending.admitted {
                        self.pending.remove(call_id);
                    }
                }
            }
            Event::RecordingStarted { .. }
            | Event::RecordingActivated
            | Event::RecordingEnded
            | Event::Protocol { .. }
            | Event::Delivery { .. }
            | Event::WorkerReady { .. }
            | Event::OutputText { .. }
            | Event::ImageOmitted { .. }
            | Event::ImageRetained { .. }
            | Event::ProcessOutput { .. }
            | Event::EnvironmentDiscovered { .. }
            | Event::StartupFailed { .. }
            | Event::PythonEnvironmentAccepted { .. }
            | Event::RequirementsAccepted { .. }
            | Event::RequirementsSelected { .. }
            | Event::CellOutput { .. }
            | Event::SessionOutput { .. } => {}
        }
        changed.sort_unstable();
        changed.dedup();
        Ok(changed)
    }
    pub(super) fn publish(
        &mut self,
        directory: &cap_std::fs::Dir,
        session: u64,
    ) -> Result<(), String> {
        let Some(mut first) = self
            .dirty
            .range((session, 0, SectionId::Startup)..)
            .take_while(|(id, _, _)| *id == session)
            .next()
            .copied()
        else {
            return Ok(());
        };
        self.dirty.retain(|(id, _, _)| *id != session);
        let fresh = self.published.as_ref().map(|(id, _)| *id) != Some(session);
        if fresh {
            // Only the most recently updated report keeps a handle. Late
            // results for an evicted session rebuild from the existing spool
            // into a private replacement; never reopen its managed file.
            self.published = None;
            let mut file = super::bundle::create_file(directory, ".session.md.tmp")
                .map_err(|e| e.to_string())?;
            writeln!(file, "# MCP Console session {session:03}\n\nRecorded source and assembled response previews.\nA transport write does not confirm that the client or model read a response.\nOmission notices are preserved; supplementary raw logs remain in Console capture and are not expanded here.\nThis live report may change while the recording is active.\n").map_err(|e| e.to_string())?;
            let end = file.stream_position().map_err(|e| e.to_string())?;
            self.published = Some((
                session,
                Published {
                    file,
                    sections: BTreeMap::new(),
                    end,
                },
            ));
            first = *self
                .order
                .range((session, 0, SectionId::Startup)..)
                .next()
                .expect("session report sections");
        }
        let published = &mut self.published.as_mut().unwrap().1;
        let key = (first.1, first.2);
        let previous = published.sections.split_off(&key);
        let anchor = previous.first_key_value();
        let resume =
            anchor.filter(|(id, _)| **id == key && self.sections.contains_key(&(session, key.1)));
        let offset = resume
            .map(|(_, section)| section.content_end)
            .or_else(|| anchor.map(|(_, section)| section.start))
            .unwrap_or(published.end);
        published
            .file
            .seek(SeekFrom::Start(offset))
            .map_err(|e| e.to_string())?;
        // Ordinary updates append only new bytes to the last section. If a
        // later result belongs to an older interval, rewrite only that suffix;
        // the preceding report is never recopied or scanned.
        for &(id, sequence, section_id) in self.order.range(first..) {
            if id != session {
                break;
            }
            let section = &self.sections[&(id, section_id)];
            if section.length == 0 {
                continue;
            }
            let prior = resume
                .filter(|(id, _)| **id == (sequence, section_id))
                .map(|(_, s)| s);
            let start = if let Some(prior) = prior {
                prior.start
            } else {
                let start = published
                    .file
                    .stream_position()
                    .map_err(|e| e.to_string())?;
                writeln!(published.file, "## {}\n", section_id.heading())
                    .map_err(|e| e.to_string())?;
                start
            };
            let mut skip = prior.map_or(0, |s| s.length);
            let mut spool = section
                .spool
                .lock()
                .map_err(|_| "report spool lock poisoned")?;
            for &(offset, length) in &section.ranges {
                let consumed = skip.min(length);
                skip -= consumed;
                if consumed == length {
                    continue;
                }
                spool
                    .seek(SeekFrom::Start(offset + consumed))
                    .map_err(|e| e.to_string())?;
                let copied = std::io::copy(
                    &mut (&mut *spool).take(length - consumed),
                    &mut published.file,
                )
                .map_err(|e| e.to_string())?;
                if copied != length - consumed {
                    return Err("report spool changed during publication".into());
                }
            }
            let content_end = published
                .file
                .stream_position()
                .map_err(|e| e.to_string())?;
            if let Some(confirmed) = section.completion {
                let note = if confirmed {
                    "Execution ended; completion confirmed."
                } else {
                    "Execution ended; completion not confirmed."
                };
                writeln!(published.file, "_{note}_\n").map_err(|e| e.to_string())?;
            }
            published.sections.insert(
                (sequence, section_id),
                PublishedSection {
                    start,
                    content_end,
                    length: section.length,
                },
            );
        }
        published.end = published
            .file
            .stream_position()
            .map_err(|e| e.to_string())?;
        published
            .file
            .set_len(published.end)
            .and_then(|()| published.file.flush())
            .map_err(|e| e.to_string())?;
        if fresh {
            directory
                .rename(".session.md.tmp", directory, "session.md")
                .map_err(|e| e.to_string())?;
        }
        Ok(())
    }
}
