use std::io::Write;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use super::{CaptureFile, Event, Interval, Owner, Transcript};
use chrono::Utc;

pub(super) const MAX_CELL_OUTPUT_BYTES: u64 = 1024 * 1024 * 1024;

/// One private append-only file containing cell or session output.
///
/// The pending-output tape remains independently bounded for MCP projection.
/// This file retains console text and direct stdout/stderr bytes before that
/// projection discards overflow. Images remain separate transcript artifacts.
pub(crate) struct CellOutput {
    file: Arc<Mutex<CaptureFile>>,
    path: String,
    owner: Owner,
    transcript: Transcript,
    failed: bool,
    retained_bytes: u64,
    record: Arc<Mutex<OutputRecordState>>,
    discarded_bytes: u64,
    retention_limit_reported: bool,
}

#[derive(Clone, Copy, Default, PartialEq)]
struct CellOutputSummary {
    retained_bytes: u64,
    inline_omitted_bytes: u64,
    discarded_bytes: u64,
}

/// One response interval's accounting receipt, shared with delivery recovery.
#[derive(Clone)]
pub(crate) struct OutputRecord {
    state: Arc<Mutex<OutputRecordState>>,
    pub(crate) public_directory: Arc<Mutex<std::path::PathBuf>>,
    pub(crate) path: Arc<str>,
    accounted: Arc<AtomicU64>,
}

struct OutputRecordState {
    transcript: Transcript,
    call_id: Option<u64>,
    owner: Owner,
    path: String,
    summary: CellOutputSummary,
    finished: bool,
    published: Option<CellOutputSummary>,
}

impl OutputRecordState {
    fn publish(&mut self) {
        if self.finished && self.published != Some(self.summary) {
            self.transcript
                .record_cell_output(self.call_id, self.owner, &self.path, self.summary);
            self.published = Some(self.summary);
        }
    }
}

impl Drop for OutputRecordState {
    fn drop(&mut self) {
        self.publish();
    }
}

impl OutputRecord {
    pub(crate) fn note_inline_omission(&self, bytes: u64) {
        let previous = self.accounted.fetch_max(bytes, Ordering::Relaxed);
        if bytes > previous {
            self.state
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .summary
                .inline_omitted_bytes += bytes - previous;
        }
    }

    pub(crate) fn publish(&self) {
        self.state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .publish();
    }
}

impl Transcript {
    pub(crate) fn create_session_output(&self) -> Result<Option<CellOutput>, String> {
        self.create_output(None)
    }
    pub(crate) fn create_cell_output(
        &self,
        call_id: Option<u64>,
    ) -> Result<Option<CellOutput>, String> {
        let Some(call_id) = call_id else {
            return Ok(None);
        };
        self.update(|state| {
            let active = state.capture()?;
            let Some(previous) = active.owner else {
                return Ok(());
            };
            let owner = Owner {
                session_id: previous.session_id,
                cell_id: Some(previous.cell_id.unwrap_or(0) + 1),
                interval: Interval::Cell,
            };
            active.snapshot_sequence = active.sequence;
            active.cell_call = Some(call_id);
            active.owner = Some(owner);
            active.image_number = 0;
            active.associate(call_id, owner.session_id)?;
            active.append(Event::CellStarted { call_id, owner }, Utc::now())
        });
        self.create_output(Some(call_id))
    }
    pub(crate) fn end_cell(&self, completion_confirmed: bool) {
        self.update(|state| {
            let active = state.capture()?;
            if let Some(owner) = active.owner.filter(|o| o.interval == Interval::Cell) {
                if let Some(call_id) = active.cell_call.take() {
                    active.append(
                        Event::CellEnded {
                            call_id,
                            owner,
                            completion_confirmed,
                        },
                        Utc::now(),
                    )?;
                }
                active.owner = Some(Owner {
                    interval: Interval::Post,
                    ..owner
                });
                active.image_number = 0;
            }
            Ok(())
        });
    }
    fn create_output(&self, call_id: Option<u64>) -> Result<Option<CellOutput>, String> {
        let (mut state, poisoned) = self.lock();
        if poisoned {
            return Err("transcript lock poisoned while creating cell output".into());
        }
        if state.failure.is_some() {
            return Ok(None);
        }
        let active = state.capture()?;
        let Some(owner) = active.owner else {
            return Ok(None);
        };
        let path = owner.path(".txt");
        let file = active.capture_file(&path)?;
        Ok(Some(CellOutput {
            file,
            path: path.clone(),
            owner,
            transcript: self.clone(),
            failed: false,
            record: Arc::new(Mutex::new(OutputRecordState {
                transcript: self.clone(),
                call_id,
                owner,
                path,
                summary: CellOutputSummary::default(),
                finished: false,
                published: None,
            })),
            retained_bytes: 0,
            discarded_bytes: 0,
            retention_limit_reported: false,
        }))
    }

    fn record_cell_output(
        &self,
        call_id: Option<u64>,
        owner: Owner,
        path: &str,
        summary: CellOutputSummary,
    ) {
        self.update(|state| {
            let event = match call_id {
                Some(call_id) => Event::CellOutput {
                    owner,
                    call_id,
                    path,
                    retained_bytes: summary.retained_bytes,
                    inline_omitted_bytes: summary.inline_omitted_bytes,
                    discarded_bytes: summary.discarded_bytes,
                    retention_limit_bytes: MAX_CELL_OUTPUT_BYTES,
                },
                None => Event::SessionOutput {
                    owner,
                    path,
                    retained_bytes: summary.retained_bytes,
                    inline_omitted_bytes: summary.inline_omitted_bytes,
                    discarded_bytes: summary.discarded_bytes,
                    retention_limit_bytes: MAX_CELL_OUTPUT_BYTES,
                },
            };
            state.capture()?.append(event, Utc::now())
        });
    }
}

impl CellOutput {
    pub(crate) fn record(&self) -> OutputRecord {
        OutputRecord {
            state: self.record.clone(),
            public_directory: self
                .transcript
                .lock()
                .0
                .active
                .as_ref()
                .expect("capture initialized")
                .public_directory
                .clone(),
            path: Arc::from(self.path.as_str()),
            accounted: Arc::new(AtomicU64::new(0)),
        }
    }

    pub(crate) fn retained_bytes(&self) -> u64 {
        self.retained_bytes
    }
    pub(crate) fn discarded_bytes(&self) -> u64 {
        self.discarded_bytes
    }
    /// Appends bytes while the retention limit permits it.
    ///
    /// Returns the retained prefix length and a server-owned notice to publish after
    /// the corresponding ordinary output event. Further writes are still
    /// drained from the worker even after persistence stops.
    pub(crate) fn append(&mut self, bytes: &[u8]) -> (usize, Option<String>) {
        if bytes.is_empty() {
            return (0, None);
        }
        if self.failed {
            self.discarded_bytes = self.discarded_bytes.saturating_add(bytes.len() as u64);
            return (0, None);
        }
        let remaining = MAX_CELL_OUTPUT_BYTES.saturating_sub(self.retained_bytes);
        let retained = bytes
            .len()
            .min(usize::try_from(remaining).unwrap_or(usize::MAX));
        let offset = self.retained_bytes;
        let result = {
            let mut file = self.file.lock().expect("capture file lock");
            file.writer
                .as_mut()
                .expect("open output interval")
                .write_all(&bytes[..retained])
        };
        if let Err(error) = result {
            let observed = std::fs::metadata(&self.file.lock().expect("capture file lock").path)
                .map(|m| m.len())
                .unwrap_or(offset);
            let appended = observed.saturating_sub(offset) as usize;
            self.retained_bytes = observed;
            self.discarded_bytes += (bytes.len() - appended) as u64;
            self.failed = true;
            self.text_event(offset, appended, bytes.len() - appended);
            return (
                appended,
                Some(format!(
                    "cell output file {} (relative to recording) stopped after {} retained bytes: {error}; later text is not retained in this file",
                    self.path, observed
                )),
            );
        }
        self.text_event(offset, retained, bytes.len() - retained);
        self.retained_bytes = self.retained_bytes.saturating_add(retained as u64);

        let discarded = bytes.len() - retained;
        if discarded == 0 {
            return (retained, None);
        }
        self.discarded_bytes = self.discarded_bytes.saturating_add(discarded as u64);
        if self.retention_limit_reported {
            return (retained, None);
        }
        self.retention_limit_reported = true;
        (
            retained,
            Some(format!(
                "cell output retention limit reached at {MAX_CELL_OUTPUT_BYTES} bytes for {} (relative to recording); later text is not retained in this file",
                self.path
            )),
        )
    }

    fn text_event(&self, offset: u64, bytes: usize, discarded_bytes: usize) {
        self.transcript.update(|state| {
            state.capture()?.append(
                Event::OutputText {
                    owner: self.owner,
                    path: &self.path,
                    offset,
                    bytes,
                    discarded_bytes,
                },
                Utc::now(),
            )
        });
    }
    pub(crate) fn flush(&mut self) -> Option<String> {
        let mut file = self.file.lock().expect("capture file lock");
        file.writer
            .as_mut()
            .and_then(|writer| writer.flush().err())
            .map(|e| format!("failed to flush {}: {e}", self.path))
    }
    pub(crate) fn finish(mut self) -> Option<String> {
        let notice = self.flush();
        let permission_error = self.file.lock().expect("capture file lock").finish().err();
        let mut record = self
            .record
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        record.summary.retained_bytes = self.retained_bytes;
        record.summary.discarded_bytes = self.discarded_bytes;
        record.finished = true;
        notice.or(permission_error)
    }
}
