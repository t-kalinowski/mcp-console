//! Incremental report projection. Result text is spooled by observed interval,
//! so later polls join their cells without retaining response history in memory.
use super::event::{Envelope, Event, Outcome, RecordedContent};
use super::{Interval, Owner};
use std::collections::BTreeMap;
use std::io::{Read as _, Seek as _, SeekFrom, Write as _};
use std::path::Path;
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
}
impl Section {
    fn new(sequence: u64, spool: Arc<Mutex<std::fs::File>>) -> Self {
        Self {
            spool,
            ranges: Vec::new(),
            sequence,
            completion: None,
        }
    }
    fn append(&mut self, bytes: &[u8]) -> Result<(), String> {
        let mut spool = self
            .spool
            .lock()
            .map_err(|_| "report spool lock poisoned")?;
        let offset = spool.seek(SeekFrom::End(0)).map_err(|e| e.to_string())?;
        spool.write_all(bytes).map_err(|e| e.to_string())?;
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
pub(super) struct Reports {
    spool: Arc<Mutex<std::fs::File>>,
    pending: BTreeMap<u64, Pending>,
    sections: BTreeMap<(u64, SectionId), Section>,
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
            current: None,
        })
    }

    fn section(&mut self, session: u64, id: SectionId, sequence: u64) -> &mut Section {
        self.sections
            .entry((session, id))
            .or_insert_with(|| Section::new(sequence, self.spool.clone()))
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
                        self.sections.insert(key, section);
                    }
                    changed.push(*session_id);
                }
            }
            Event::CellStarted { call_id, owner } => {
                let key = (owner.session_id, SectionId::owner(*owner));
                if let Some(pending) = self.pending.get_mut(call_id)
                    && let Some(previous) = pending.target.replace(key)
                    && let Some(mut section) = self.sections.remove(&previous)
                {
                    section.sequence = sequence;
                    self.sections.insert(key, section);
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
    pub(super) fn publish(&mut self, directory: &Path, session: u64) -> Result<(), String> {
        let destination = directory.join(format!("sessions/session-{session:03}/session.md"));
        let mut temporary =
            tempfile::NamedTempFile::new_in(destination.parent().expect("bundle directory"))
                .map_err(|e| e.to_string())?;
        writeln!(temporary, "# MCP Console session {session:03}\n\nRecorded source and assembled response previews.\nA transport write does not confirm that the client or model read a response.\nOmission notices are preserved; supplementary raw logs remain in Console capture and are not expanded here.\nThis live report may change while the recording is active.\n").map_err(|e| e.to_string())?;
        let mut sections: Vec<_> = self
            .sections
            .iter_mut()
            .filter(|((id, _), _)| *id == session)
            .collect();
        sections.sort_by_key(|(_, section)| section.sequence);
        for ((_, key), section) in sections {
            if section.ranges.is_empty() {
                continue;
            }
            writeln!(temporary, "## {}\n", key.heading()).map_err(|e| e.to_string())?;
            let mut spool = section
                .spool
                .lock()
                .map_err(|_| "report spool lock poisoned")?;
            for &(offset, length) in &section.ranges {
                spool
                    .seek(SeekFrom::Start(offset))
                    .map_err(|e| e.to_string())?;
                let copied = std::io::copy(&mut (&mut *spool).take(length), &mut temporary)
                    .map_err(|e| e.to_string())?;
                if copied != length {
                    return Err("report spool changed during publication".into());
                }
            }
            if let Some(confirmed) = section.completion {
                let note = if confirmed {
                    "Execution ended; completion confirmed."
                } else {
                    "Execution ended; completion not confirmed."
                };
                writeln!(temporary, "_{note}_\n").map_err(|e| e.to_string())?;
            }
        }
        temporary.persist(&destination).map_err(|e| e.to_string())?;
        Ok(())
    }
}
