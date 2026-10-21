//! One Console process's capture and derived documents. Before send, capture
//! lives in temporary storage; publication never changes event time or ownership.
use std::collections::BTreeMap;
use std::fs::{DirBuilder, File, OpenOptions};
use std::io::{BufWriter, Write};
#[cfg(unix)]
use std::os::unix::fs::{DirBuilderExt as _, OpenOptionsExt as _};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use base64::Engine as _;
use chrono::{DateTime, SecondsFormat, Utc};
use event::{Envelope, Event, Outcome, RecordedContent, RecordedResult};
use rmcp::{
    ErrorData,
    model::{
        CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, RequestId,
        RequestMetaObject,
    },
};
use serde::Serialize;
use serde_json::Value;

mod event;
mod markdown;
mod output;
pub(crate) use output::{CellOutput, OutputRecord};

/// Retained process diagnostics have no worker interval or raw text file.
pub(crate) struct ProcessOutput {
    pub public_directory: Arc<Mutex<PathBuf>>,
    pub retained_bytes: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub(crate) struct Owner {
    pub session_id: u64,
    pub cell_id: Option<u64>,
    pub interval: Interval,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Interval {
    Startup,
    Cell,
    Post,
}
impl Owner {
    fn stem(self) -> String {
        match self.interval {
            Interval::Startup => "startup".into(),
            Interval::Cell => format!("cell-{:03}", self.cell_id.expect("cell interval")),
            Interval::Post => format!("cell-{:03}-post", self.cell_id.expect("post interval")),
        }
    }
    fn path(self, suffix: &str) -> String {
        format!(
            "capture/sessions/{:03}/{}{suffix}",
            self.session_id,
            self.stem()
        )
    }
}
/// Byte ranges in the actual assembled text blocks, or one image block.
#[derive(Clone, Serialize)]
pub(crate) struct ResultPart {
    pub block: usize,
    pub start: usize,
    pub end: usize,
    pub owner: Option<Owner>,
}

#[derive(Clone)]
pub(crate) struct Transcript(
    Arc<Mutex<TranscriptState>>,
    Arc<Mutex<ProtocolEvents>>,
    Arc<AtomicU64>,
);
#[derive(Default)]
struct ProtocolEvents {
    pending: Vec<(QueuedEvent, DateTime<Utc>, usize)>,
    // Include batches held by flush_protocol while journal I/O is blocked.
    retained_events: usize,
    retained_bytes: usize,
    failure: Option<String>,
    draining: bool,
}

// Lifecycle observations share the bounded, ordered drain with protocol events.
// Session identity changes only when an actual acquisition is applied.
#[derive(serde::Serialize)]
enum QueuedEvent {
    Event(Event<'static>),
    WorkerStarting,
    WorkerStarted { pid: u32, requirements: Value },
    WorkerReady,
    WorkerEnded { outcome: String },
}

impl QueuedEvent {
    fn append(self, active: &mut ActiveTranscript, at: DateTime<Utc>) -> Result<(), String> {
        match self {
            Self::Event(event) => active.append(event, at),
            Self::WorkerStarting => {
                active.starting_session = None;
                Ok(())
            }
            Self::WorkerStarted { pid, requirements } => {
                active.next_session_id += 1;
                let session_id = active.next_session_id;
                active.owner = Some(Owner {
                    session_id,
                    cell_id: None,
                    interval: Interval::Startup,
                });
                active.image_number = 0;
                active.starting_session = Some(session_id);
                active.append(
                    Event::WorkerStarted {
                        session_id,
                        pid,
                        requirements: &requirements,
                    },
                    at,
                )
            }
            Self::WorkerReady => {
                let session_id = active
                    .starting_session
                    .take()
                    .ok_or("worker ready without a started session")?;
                active.append(Event::WorkerReady { session_id }, at)
            }
            Self::WorkerEnded { outcome } => {
                let session_id = active
                    .owner
                    .take()
                    .ok_or("worker outcome without a started session")?
                    .session_id;
                active.append(
                    Event::WorkerEnded {
                        session_id,
                        outcome: &outcome,
                    },
                    at,
                )
            }
        }
    }
}

const MAX_PROTOCOL_EVENTS: usize = 1024;
const MAX_PROTOCOL_BYTES: usize = 8 * 1024 * 1024;

impl ProtocolEvents {
    fn stop(&mut self, error: String) {
        self.failure.get_or_insert(error);
        self.retained_events -= self.pending.len();
        self.retained_bytes -= self
            .pending
            .drain(..)
            .map(|(_, _, bytes)| bytes)
            .sum::<usize>();
    }
}

/// Measure serialized retention without allocating another copy of the payload.
#[derive(Default)]
struct ProtocolBytes(usize);

impl Write for ProtocolBytes {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0 += bytes.len();
        Ok(bytes.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

struct TranscriptState {
    started_at: DateTime<Utc>,
    working_directory: Result<PathBuf, String>,
    dynamic_resolution: bool,
    python_preparation: bool,
    r_available: bool,
    startup_requirements: crate::worker_client::Declaration,
    metadata_known: bool,
    active: Option<ActiveTranscript>,
    failure: Option<String>,
}
struct ActiveTranscript {
    temporary: Option<tempfile::TempDir>,
    directory: PathBuf,
    public_directory: Arc<Mutex<PathBuf>>,
    writer: BufWriter<File>,
    files: BTreeMap<String, std::sync::Weak<Mutex<CaptureFile>>>,
    projections: Option<markdown::Writers>,
    pending_projection_failure: Option<String>,
    run_id: String,
    sequence: u64,
    next_artifact_id: u64,
    next_session_id: u64,
    owner: Option<Owner>,
    image_number: u64,
    cell_call: Option<u64>,
    finished: bool,
    starting_session: Option<u64>,
    process_output_bytes: u64,
}
/// Writers share this handle so publication can move temporary output without
/// changing the output tape's ownership or reopening a completed interval.
struct CaptureFile {
    writer: Option<File>,
    path: PathBuf,
    complete: bool,
}
#[derive(Clone)]
pub(crate) struct Call {
    id: Option<u64>,
    result_images: Arc<Mutex<Option<Vec<Artifact>>>>,
    parts: Arc<Mutex<Vec<ResultPart>>>,
}
#[derive(Clone)]
pub(crate) struct Artifact {
    id: u64,
    pub(crate) path: String,
    mime_type: String,
    pub(crate) owner: Option<Owner>,
    pub(crate) public_directory: Arc<Mutex<PathBuf>>,
}
impl Transcript {
    #[cfg(test)]
    pub(crate) fn new(dynamic_resolution: bool) -> Self {
        Self::configured(
            std::env::current_dir(),
            dynamic_resolution,
            false,
            true,
            crate::worker_client::Declaration::default(),
        )
    }
    pub(crate) fn configured(
        working_directory: std::io::Result<PathBuf>,
        dynamic_resolution: bool,
        python_preparation: bool,
        r_available: bool,
        startup_requirements: crate::worker_client::Declaration,
    ) -> Self {
        Self(
            Arc::new(Mutex::new(TranscriptState {
                started_at: Utc::now(),
                working_directory: working_directory
                    .map_err(|e| format!("failed to find the current working directory: {e}")),
                dynamic_resolution,
                python_preparation,
                r_available,
                startup_requirements,
                metadata_known: true,
                active: None,
                failure: None,
            })),
            Arc::default(),
            Arc::default(),
        )
    }
    pub(crate) fn pending(working_directory: std::io::Result<PathBuf>) -> Self {
        let value = Self::configured(working_directory, false, false, false, Default::default());
        value.0.lock().expect("transcript lock").metadata_known = false;
        value
    }
    pub(crate) fn configure(&self, configured: Self) {
        let (configuration, _) = configured.lock();
        self.update(|state| {
            state.capture()?;
            state.dynamic_resolution = configuration.dynamic_resolution;
            state.python_preparation = configuration.python_preparation;
            state.r_available = configuration.r_available;
            state.startup_requirements = configuration.startup_requirements.clone();
            state.metadata_known = true;
            if let Some(projections) = state.active.as_mut().and_then(|a| a.projections.as_mut()) {
                projections.configure(
                    configuration.dynamic_resolution,
                    configuration.r_available,
                    &configuration.startup_requirements,
                );
            }
            state.capture()?.append(
                Event::EnvironmentDiscovered {
                    dynamic_resolution: configuration.dynamic_resolution,
                    python_preparation: configuration.python_preparation,
                    r_available: configuration.r_available,
                    startup_requirements: &configuration.startup_requirements,
                },
                Utc::now(),
            )
        });
    }
    pub(crate) fn activate(&self) {
        self.update(|state| {
            let working_directory = state.working_directory.clone()?;
            state.capture()?.publish(&working_directory)
        });
    }
    pub(crate) fn protocol(&self, direction: &'static str, message: Value) {
        self.queue_event(QueuedEvent::Event(Event::Protocol { direction, message }));
    }

    pub(crate) fn delivery(&self, request_id: &Value, call_id: Option<u64>, delivered: bool) {
        self.queue_event(QueuedEvent::Event(Event::Delivery {
            request_id: request_id.clone(),
            call_id,
            delivered,
        }));
    }

    fn queue_event(&self, event: QueuedEvent) {
        if self
            .1
            .lock()
            .expect("protocol capture lock")
            .failure
            .is_some()
        {
            return;
        }
        let at = Utc::now();
        let mut bytes = ProtocolBytes::default();
        let measured = serde_json::to_writer(&mut bytes, &event);
        let mut events = self.1.lock().expect("protocol capture lock");
        if events.failure.is_some() {
            return;
        }
        if let Err(error) = measured {
            events.stop(format!("protocol capture failed: {error}"));
        } else if events.retained_events == MAX_PROTOCOL_EVENTS
            || bytes.0 > MAX_PROTOCOL_BYTES - events.retained_bytes
        {
            // This lock never protects journal I/O. The drain reports the failure
            // after its current write settles; frame reception remains fail-open.
            events.stop(format!(
                "protocol capture backlog exceeded ({MAX_PROTOCOL_EVENTS} events or {MAX_PROTOCOL_BYTES} bytes)"
            ));
        } else {
            events.retained_events += 1;
            events.retained_bytes += bytes.0;
            events.pending.push((event, at, bytes.0));
        }
        if events.draining {
            return;
        }
        events.draining = true;
        drop(events);
        let recorder = self.clone();
        // Neither journal I/O nor the recorder mutex runs on MCP frame receipt.
        // One retained drain owns this queue, including cancelled deliveries.
        std::thread::spawn(move || {
            loop {
                recorder.update(|_| Ok(()));
                let mut events = recorder.1.lock().expect("protocol capture lock");
                if events.pending.is_empty() {
                    events.draining = false;
                    return;
                }
            }
        });
    }

    fn flush_protocol(&self, state: &mut TranscriptState) -> Result<(), String> {
        let pending = {
            let mut events = self.1.lock().expect("protocol capture lock");
            if let Some(error) = &events.failure {
                return Err(error.clone());
            }
            std::mem::take(&mut events.pending)
        };
        let count = pending.len();
        let bytes = pending.iter().map(|(_, _, bytes)| bytes).sum::<usize>();
        let result = (|| {
            for (event, at, _) in pending {
                event.append(state.capture()?, at)?;
            }
            Ok(())
        })();
        let mut events = self.1.lock().expect("protocol capture lock");
        events.retained_events -= count;
        events.retained_bytes -= bytes;
        result
    }

    pub(crate) fn requirements_selected(
        &self,
        call_id: Option<u64>,
        action: &str,
        snapshot: &Value,
    ) {
        self.update(|state| {
            state.capture()?.append(
                Event::RequirementsSelected {
                    call_id,
                    action,
                    snapshot,
                },
                Utc::now(),
            )
        });
    }
    pub(crate) fn requirements_accepted(&self, requirements: &Value) {
        self.update(|state| {
            state
                .capture()?
                .append(Event::RequirementsAccepted { requirements }, Utc::now())
        });
    }
    pub(crate) fn startup_failed(&self, message: &str) {
        self.update(|state| {
            let active = state.capture()?;
            let session_id = active.starting_session.take();
            active.append(
                Event::StartupFailed {
                    session_id,
                    message,
                },
                Utc::now(),
            )
        });
    }
    pub(crate) fn python_environment_accepted(&self, packages: &[String]) {
        self.update(|state| {
            state
                .capture()?
                .append(Event::PythonEnvironmentAccepted { packages }, Utc::now())
        });
    }
    pub(crate) fn worker_starting(&self) {
        self.queue_event(QueuedEvent::WorkerStarting);
    }

    pub(crate) fn worker_started(&self, pid: u32, requirements: &Value) {
        self.queue_event(QueuedEvent::WorkerStarted {
            pid,
            requirements: requirements.clone(),
        });
    }

    pub(crate) fn worker_ready(&self) {
        self.queue_event(QueuedEvent::WorkerReady);
    }

    pub(crate) fn worker_ended(&self, outcome: &str) {
        self.queue_event(QueuedEvent::WorkerEnded {
            outcome: outcome.to_owned(),
        });
    }

    pub(crate) fn image_omitted(&self, encoded_bytes: usize, mime_type: &str) {
        self.update(|state| {
            let active = state.capture()?;
            active.append(
                Event::ImageOmitted {
                    owner: active.owner,
                    encoded_bytes,
                    mime_type,
                },
                Utc::now(),
            )
        });
    }
    pub(crate) fn owner(&self) -> Option<Owner> {
        self.update(|state| Ok(state.active.as_ref().and_then(|a| a.owner)))
            .flatten()
    }
    /// Reserve protocol identity without waiting for the journal writer.
    pub(crate) fn reserve_call_id(&self) -> u64 {
        self.2
            .fetch_add(1, Ordering::Relaxed)
            .checked_add(1)
            .expect("recording call ID space exhausted")
    }
    pub(crate) fn begin(
        &self,
        call_id: u64,
        at: DateTime<Utc>,
        request_id: &RequestId,
        request_meta: &RequestMetaObject,
        request: &CallToolRequestParams,
    ) -> Call {
        self.activate();
        self.update(|state| {
            let active = state.capture()?;
            let mut request = request.clone();
            if !request_meta.is_empty() {
                request.meta = Some(request_meta.clone());
            }
            active.append(
                Event::ToolCall {
                    call_id,
                    request_id,
                    request: &request,
                },
                at,
            )?;
            Ok(Call {
                id: Some(call_id),
                result_images: Arc::new(Mutex::new(None)),
                parts: Arc::new(Mutex::new(Vec::new())),
            })
        })
        .unwrap_or_else(Call::unrecorded)
    }
    /// Attachment may be interrupted before execution. Record the selected
    /// worker without creating a cell interval or retaining the request here.
    pub(crate) fn source_attached(&self, call_id: Option<u64>) {
        self.update(|state| {
            let active = state.capture()?;
            if let (Some(call_id), Some(owner)) = (call_id, active.owner) {
                active.append(
                    Event::SourceAttached {
                        call_id,
                        session_id: owner.session_id,
                    },
                    Utc::now(),
                )?;
            }
            Ok(())
        });
    }

    pub(crate) fn finish(&self, call: Call, response: &Result<CallToolResponse, ErrorData>) {
        let Some(call_id) = call.id else {
            return;
        };
        self.update(|state| {
            let images = call.take_result_images()?;
            let parts = call
                .parts
                .lock()
                .map_err(|_| "tool result association lock poisoned")?;
            let active = state.capture()?;
            active.finish(call_id, images, &parts, response, Utc::now())
        });
    }
    pub(crate) fn persist_decoded_image(
        &self,
        call_id: Option<u64>,
        bytes: &[u8],
        mime_type: &str,
    ) -> Option<Artifact> {
        self.update(|state| state.capture()?.persist_image(call_id, bytes, mime_type))
    }
    pub(crate) fn persist_session_image(
        &self,
        data: &str,
        mime_type: &str,
    ) -> Result<Option<Artifact>, String> {
        let bytes = decode_image_data(data)?;
        Ok(self.persist_decoded_image(None, &bytes, mime_type))
    }
    pub(crate) fn unowned_text(&self, bytes: &[u8]) -> Option<ProcessOutput> {
        self.update(|state| {
            let active = state.capture()?;
            let retained = bytes.len().min(
                output::MAX_CELL_OUTPUT_BYTES.saturating_sub(active.process_output_bytes) as usize,
            );
            active.process_output_bytes += retained as u64;
            active.append(
                Event::ProcessOutput {
                    data: &base64::engine::general_purpose::STANDARD.encode(&bytes[..retained]),
                    discarded_bytes: bytes.len() - retained,
                },
                Utc::now(),
            )?;
            Ok(ProcessOutput {
                public_directory: active.public_directory.clone(),
                retained_bytes: retained,
            })
        })
    }
    pub(crate) fn finalize(&self) {
        self.update(|state| {
            if let Some(active) = state.active.as_mut()
                && !active.finished
            {
                active.append(Event::RecordingEnded, Utc::now())?;
            }
            Ok(())
        });
        // Failed capture must not gain a completion event, but its producers
        // have still retired. Complete the permissions on retained files too.
        let (mut state, _) = self.lock();
        let result = state
            .active
            .as_mut()
            .map(ActiveTranscript::finalize)
            .transpose();
        drop(state);
        if let Err(error) = result {
            report_failure(&error);
        }
    }
    pub(crate) fn disable(&self, error: String) {
        let (mut state, _) = self.lock();
        let should_report = state.disable(error.clone());
        drop(state);
        self.1
            .lock()
            .expect("protocol capture lock")
            .stop(error.clone());
        if should_report {
            report_failure(&error);
        }
    }

    fn lock(&self) -> (std::sync::MutexGuard<'_, TranscriptState>, bool) {
        match self.0.lock() {
            Ok(state) => (state, false),
            Err(poisoned) => (poisoned.into_inner(), true),
        }
    }

    fn update<T>(
        &self,
        operation: impl FnOnce(&mut TranscriptState) -> Result<T, String>,
    ) -> Option<T> {
        let (mut state, poisoned) = self.lock();
        if let Some(error) = &state.failure {
            self.1
                .lock()
                .expect("protocol capture lock")
                .stop(error.clone());
            return None;
        }
        if poisoned {
            let error = "transcript lock poisoned".to_string();
            state.disable(error.clone());
            drop(state);
            self.1
                .lock()
                .expect("protocol capture lock")
                .stop(error.clone());
            report_failure(&error);
            return None;
        }
        let result = self
            .flush_protocol(&mut state)
            .and_then(|()| operation(&mut state))
            .and_then(|value| {
                self.flush_protocol(&mut state)?;
                Ok(value)
            });
        match result {
            Ok(value) => {
                let projection_failure = state
                    .active
                    .as_mut()
                    .and_then(|a| a.pending_projection_failure.take());
                drop(state);
                if let Some(error) = projection_failure {
                    report_projection_failure(&error);
                }
                Some(value)
            }
            Err(error) => {
                state.disable(error.clone());
                drop(state);
                self.1
                    .lock()
                    .expect("protocol capture lock")
                    .stop(error.clone());
                report_failure(&error);
                None
            }
        }
    }
}

pub(crate) fn validate_image_data(data: &str) -> Result<(), String> {
    decode_image_data(data).map(drop)
}
pub(crate) fn decode_image_data(data: &str) -> Result<Vec<u8>, String> {
    base64::engine::general_purpose::STANDARD
        .decode(data)
        .map_err(|e| format!("worker returned invalid base64 image data: {e}"))
}
impl TranscriptState {
    fn capture(&mut self) -> Result<&mut ActiveTranscript, String> {
        if self.active.is_none() {
            let working_directory = self.working_directory.clone()?;
            self.active = Some(ActiveTranscript::create(
                &working_directory,
                self.started_at,
                self.dynamic_resolution,
                self.python_preparation,
                self.r_available,
                &self.startup_requirements,
                self.metadata_known,
            )?);
        }
        self.active()
    }

    fn active(&mut self) -> Result<&mut ActiveTranscript, String> {
        self.active
            .as_mut()
            .ok_or_else(|| "transcript was not materialized before recording output".to_string())
    }

    fn disable(&mut self, error: String) -> bool {
        if self.failure.is_some() {
            return false;
        }
        self.failure = Some(error);
        true
    }
}

impl ActiveTranscript {
    fn create(
        working_directory: &Path,
        started_at: DateTime<Utc>,
        dynamic_resolution: bool,
        python_preparation: bool,
        r_available: bool,
        startup_requirements: &crate::worker_client::Declaration,
        metadata_known: bool,
    ) -> Result<Self, String> {
        let working_directory_text = working_directory.to_string_lossy();
        // Keep incidental process-ID widths from shifting bounded output previews.
        let run_id = format!(
            "{}-{:010}",
            started_at.format("%Y%m%dT%H%M%S%.9fZ"),
            std::process::id()
        );
        let temporary = tempfile::tempdir()
            .map_err(|error| format!("failed to create temporary capture: {error}"))?;
        let directory = temporary.path().to_owned();
        create_private_directory(&directory.join("capture/sessions"), true)
            .map_err(|error| format!("failed to create capture directory: {error}"))?;
        let journal = directory.join("capture/events.jsonl");
        let writer = create_private_file(&journal)
            .map(BufWriter::new)
            .map_err(|error| format!("failed to create {}: {error}", journal.display()))?;
        let projections = (|| {
            let working_directory = working_directory.to_str().ok_or_else(|| {
                "Quarto execution root requires a UTF-8 working directory".to_string()
            })?;
            let markdown_path = directory.join("transcript.md");
            let markdown = create_private_file(&markdown_path).map_err(|error| {
                format!("failed to create {}: {error}", markdown_path.display())
            })?;
            let quarto_path = directory.join("transcript.qmd");
            let quarto = create_private_file(&quarto_path)
                .map_err(|error| format!("failed to create {}: {error}", quarto_path.display()))?;
            drop(quarto);
            Ok(markdown::Writers::new(
                markdown,
                quarto_path,
                working_directory,
                dynamic_resolution,
                r_available,
                startup_requirements,
            ))
        })();
        let (projections, pending_projection_failure) = match projections {
            Ok(projections) => (Some(projections), None),
            Err(error) => (None, Some(error)),
        };

        let mut transcript = Self {
            temporary: Some(temporary),
            directory,
            public_directory: Arc::default(),
            files: BTreeMap::new(),
            writer,
            projections,
            pending_projection_failure,
            run_id,
            sequence: 0,
            next_artifact_id: 0,
            next_session_id: 0,
            owner: None,
            image_number: 0,
            cell_call: None,
            process_output_bytes: 0,
            starting_session: None,
            finished: false,
        };
        transcript.append(
            Event::RecordingStarted {
                working_directory: &working_directory_text,
                dynamic_resolution: metadata_known.then_some(dynamic_resolution),
                python_preparation: metadata_known.then_some(python_preparation),
                startup_requirements: metadata_known.then_some(startup_requirements),
            },
            started_at,
        )?;
        Ok(transcript)
    }
}

impl ActiveTranscript {
    fn publish(&mut self, working_directory: &Path) -> Result<(), String> {
        if self.temporary.is_none() {
            return Ok(());
        }
        let project_console = working_directory.join(".agents/console");
        let in_project = match std::fs::metadata(&project_console) {
            Ok(metadata) => metadata.is_dir(),
            Err(e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
                ) =>
            {
                false
            }
            Err(e) => return Err(format!("cannot inspect {}: {e}", project_console.display())),
        };
        let console = if in_project {
            project_console
        } else {
            crate::console_paths::home_console_directory()?.ok_or("HOME is not set")?
        };
        let sessions = console.join("sessions");
        let directory = sessions.join(&self.run_id);
        let public = if in_project {
            PathBuf::from(".agents/console/sessions").join(&self.run_id)
        } else {
            directory.clone()
        };
        if public.to_str().is_none() {
            return Err("recording path must be UTF-8".into());
        }
        create_private_directory(&sessions, true)
            .map_err(|e| format!("failed to create {}: {e}", sessions.display()))?;
        create_private_directory(&directory, false)
            .map_err(|e| format!("failed to create {}: {e}", directory.display()))?;
        self.writer.flush().map_err(|e| e.to_string())?;
        // The tape can be appending while the transport activates recording.
        // Hold each shared writer across its copy and relocation.
        for (relative, file) in &self.files {
            let Some(file) = file.upgrade() else { continue };
            let mut file = file.lock().map_err(|_| "capture file lock poisoned")?;
            file.relocate(&directory.join(relative))?;
        }
        copy_capture(&self.directory, &directory)?;
        self.writer = BufWriter::new(
            OpenOptions::new()
                .append(true)
                .open(directory.join("capture/events.jsonl"))
                .map_err(|e| e.to_string())?,
        );
        if let Some(projections) = &mut self.projections
            && let Err(error) = projections.relocate(&directory)
        {
            self.projections = None;
            self.pending_projection_failure = Some(error);
        }
        self.directory = directory;
        *self
            .public_directory
            .lock()
            .map_err(|_| "recording path lock poisoned")? = public;
        if let Some(temporary) = self.temporary.take() {
            discard_temporary_capture(temporary);
        }
        self.append(Event::RecordingActivated, Utc::now())
    }
    fn append(&mut self, event: Event<'_>, at: DateTime<Utc>) -> Result<(), String> {
        if self.finished {
            return Ok(());
        }
        self.sequence += 1;
        let envelope = Envelope {
            event,
            run_id: &self.run_id,
            sequence: self.sequence,
            at: timestamp(at),
        };
        let mut bytes = serde_json::to_vec(&envelope)
            .map_err(|e| format!("failed to serialize transcript event: {e}"))?;
        bytes.push(b'\n');
        self.writer
            .write_all(&bytes)
            .and_then(|()| self.writer.flush())
            .map_err(|e| format!("failed to append transcript event: {e}"))?;
        if let Some(projections) = &mut self.projections
            && let Err(error) = projections.append(&envelope)
        {
            self.projections = None;
            self.pending_projection_failure = Some(error);
        }
        Ok(())
    }
    fn capture_file(&mut self, relative: &str) -> Result<Arc<Mutex<CaptureFile>>, String> {
        let path = self.directory.join(relative);
        let public_path = self
            .public_directory
            .lock()
            .map_err(|_| "recording path lock poisoned")?
            .join(relative);
        create_private_directory(path.parent().expect("capture parent"), true)
            .map_err(|e| e.to_string())?;
        let writer = create_private_file(&path)
            .map_err(|e| format!("failed to create {}: {e}", public_path.display()))?;
        let file = Arc::new(Mutex::new(CaptureFile {
            writer: Some(writer),
            path,
            complete: false,
        }));
        self.files.retain(|_, file| file.strong_count() != 0);
        self.files
            .insert(relative.to_string(), Arc::downgrade(&file));
        Ok(file)
    }
    fn persist_image(
        &mut self,
        call_id: Option<u64>,
        bytes: &[u8],
        mime_type: &str,
    ) -> Result<Artifact, String> {
        let owner = self.owner.ok_or("worker image has no worker session")?;
        self.next_artifact_id += 1;
        self.image_number += 1;
        let id = self.next_artifact_id;
        let extension = if mime_type == "image/png" {
            "png"
        } else {
            "bin"
        };
        let path = owner.path(&format!("-{:03}.{extension}", self.image_number));
        let file = self.capture_file(&path)?;
        {
            let mut file = file.lock().map_err(|_| "image lock poisoned")?;
            file.writer
                .as_mut()
                .expect("image writer")
                .write_all(bytes)
                .map_err(|e| e.to_string())?;
            file.finish()?;
        }
        self.append(
            Event::ArtifactCreated {
                artifact_id: id,
                call_id,
                owner,
                path: &path,
                mime_type,
                bytes: bytes.len(),
            },
            Utc::now(),
        )?;
        Ok(Artifact {
            id,
            path,
            mime_type: mime_type.into(),
            owner: Some(owner),
            public_directory: self.public_directory.clone(),
        })
    }
    fn finish(
        &mut self,
        call_id: u64,
        result_images: Vec<Artifact>,
        parts: &[ResultPart],
        response: &Result<CallToolResponse, ErrorData>,
        at: DateTime<Utc>,
    ) -> Result<(), String> {
        let outcome = match response {
            Ok(CallToolResponse::Complete(result)) => Outcome::Result {
                result: Self::project_result(call_id, result_images, result)?,
            },
            Ok(_) => {
                return Err("console tool unexpectedly returned a non-final MCP response".into());
            }
            Err(error) => Outcome::Error { error },
        };
        self.append(
            Event::ToolResult {
                call_id,
                parts,
                outcome,
            },
            at,
        )
    }
    fn finalize(&mut self) -> Result<(), String> {
        if self.finished {
            return Ok(());
        }
        let mut errors = Vec::new();
        self.finished = true;
        // Retirement and response-delivery settling precede this call.
        for file in self.files.values().filter_map(std::sync::Weak::upgrade) {
            if let Err(error) = file
                .lock()
                .map_err(|_| "capture file lock poisoned".to_string())
                .and_then(|mut f| f.finish())
            {
                errors.push(error);
            }
        }
        // Projection writes flush per event; drop the producer before chmod.
        self.projections.take();
        self.files.clear();
        // A projection may have failed after creating some files. Its writer is
        // now gone; finalize those partial files as well as the usable capture.
        if let Err(error) = finalize_files(&self.directory) {
            errors.push(error);
        }
        if errors.is_empty() {
            Ok(())
        } else {
            Err(errors.join("; "))
        }
    }
    fn project_result(
        call_id: u64,
        result_images: Vec<Artifact>,
        result: &CallToolResult,
    ) -> Result<RecordedResult<'_>, String> {
        let mut result_images = result_images.into_iter();
        let mut content = Vec::with_capacity(result.content.len());
        for item in &result.content {
            content.push(match item {
                ContentBlock::Text(text) => RecordedContent::Text(text),
                ContentBlock::Image(image) => {
                    let artifact = result_images.next().ok_or_else(|| {
                        format!("tool call {call_id} returned an image without a retained artifact")
                    })?;
                    if image.mime_type != artifact.mime_type {
                        return Err(format!(
                            "tool call {call_id} returned an image with a different MIME type than its artifact"
                        ));
                    }
                    RecordedContent::Image {
                        mime_type: &image.mime_type,
                        meta: image.meta.as_ref(),
                        annotations: image.annotations.as_ref(),
                        artifact_id: artifact.id,
                        path: artifact.path,
                    }
                }
                other => RecordedContent::Other(other),
            });
        }
        if result_images.next().is_some() {
            return Err(format!(
                "tool call {call_id} retained more artifacts than it returned"
            ));
        }
        Ok(RecordedResult {
            content,
            structured_content: result.structured_content.as_ref(),
            is_error: result.is_error,
            meta: result.meta.as_ref(),
        })
    }
}

impl Call {
    pub(crate) fn unrecorded() -> Self {
        Self {
            id: None,
            result_images: Arc::new(Mutex::new(None)),
            parts: Arc::new(Mutex::new(Vec::new())),
        }
    }

    pub(crate) fn id(&self) -> Option<u64> {
        self.id
    }

    pub(crate) fn record_result_images(&self, images: Vec<Artifact>) -> Result<(), String> {
        let mut result_images = self
            .result_images
            .lock()
            .map_err(|_| "tool call artifact lock poisoned".to_string())?;
        if result_images.is_some() {
            return Err(format!(
                "tool call {} already retained result images",
                self.id.unwrap_or_default()
            ));
        }
        *result_images = Some(images);
        Ok(())
    }

    fn take_result_images(&self) -> Result<Vec<Artifact>, String> {
        Ok(self
            .result_images
            .lock()
            .map_err(|_| "tool call artifact lock poisoned".to_string())?
            .take()
            .unwrap_or_default())
    }
}

impl Call {
    pub(crate) fn record_result_parts(&self, parts: Vec<ResultPart>) {
        *self.parts.lock().expect("tool result association lock") = parts;
    }
}
impl Drop for ActiveTranscript {
    fn drop(&mut self) {
        if let Some(temporary) = self.temporary.take() {
            discard_temporary_capture(temporary);
        }
    }
}

fn discard_temporary_capture(temporary: tempfile::TempDir) {
    // Windows deletion can reject read-only files. This tree is private scratch
    // storage; clearing the flag here never changes the published recording.
    #[cfg(windows)]
    // This changes the Windows file attribute, preserving its existing ACL.
    // The lint's world-writable Unix behavior cannot occur in this function.
    #[allow(clippy::permissions_set_readonly_false)]
    fn clear_readonly(directory: &Path) -> std::io::Result<()> {
        for entry in std::fs::read_dir(directory)? {
            let entry = entry?;
            if entry.file_type()?.is_dir() {
                clear_readonly(&entry.path())?;
            } else {
                let mut permissions = entry.metadata()?.permissions();
                permissions.set_readonly(false);
                std::fs::set_permissions(entry.path(), permissions)?;
            }
        }
        Ok(())
    }
    #[cfg(windows)]
    if let Err(error) = clear_readonly(temporary.path()) {
        let _ = writeln!(
            std::io::stderr().lock(),
            "mcp-console: temporary recording cleanup failed: {error}"
        );
    }
    if let Err(error) = temporary.close() {
        let _ = writeln!(
            std::io::stderr().lock(),
            "mcp-console: temporary recording cleanup failed: {error}"
        );
    }
}
impl CaptureFile {
    fn finish(&mut self) -> Result<(), String> {
        if self.complete {
            return Ok(());
        }
        if let Some(mut writer) = self.writer.take() {
            writer.flush().map_err(|e| e.to_string())?;
        }
        make_read_only(&self.path)?;
        self.complete = true;
        Ok(())
    }
    fn relocate(&mut self, destination: &Path) -> Result<(), String> {
        if let Some(writer) = &mut self.writer {
            writer.flush().map_err(|e| e.to_string())?;
        }
        create_private_directory(destination.parent().expect("capture parent"), true)
            .map_err(|e| e.to_string())?;
        std::fs::copy(&self.path, destination).map_err(|e| e.to_string())?;
        self.writer = if self.complete {
            None
        } else {
            Some(
                OpenOptions::new()
                    .append(true)
                    .open(destination)
                    .map_err(|e| e.to_string())?,
            )
        };
        self.path = destination.to_owned();
        Ok(())
    }
}
fn copy_capture(source: &Path, destination: &Path) -> Result<(), String> {
    for entry in std::fs::read_dir(source).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let target = destination.join(entry.file_name());
        if entry.file_type().map_err(|e| e.to_string())?.is_dir() {
            create_private_directory(&target, true).map_err(|e| e.to_string())?;
            copy_capture(&entry.path(), &target)?;
        } else if !target.exists() {
            std::fs::copy(entry.path(), target).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}
fn finalize_files(directory: &Path) -> Result<(), String> {
    let mut errors = Vec::new();
    for entry in std::fs::read_dir(directory).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let kind = entry.file_type().map_err(|e| e.to_string())?;
        let result = if kind.is_dir() {
            finalize_files(&entry.path())
        } else if kind.is_file() {
            make_read_only(&entry.path())
        } else {
            Ok(())
        };
        if let Err(error) = result {
            errors.push(error);
        }
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join("; "))
    }
}
fn make_read_only(path: &Path) -> Result<(), String> {
    let mut permissions = std::fs::metadata(path)
        .map_err(|e| e.to_string())?
        .permissions();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt as _;
        permissions.set_mode(permissions.mode() & !0o222);
    }
    #[cfg(not(unix))]
    permissions.set_readonly(true);
    std::fs::set_permissions(path, permissions)
        .map_err(|e| format!("failed to make {} read-only: {e}", path.display()))
}
fn timestamp(at: DateTime<Utc>) -> String {
    at.to_rfc3339_opts(SecondsFormat::Nanos, true)
}

fn report_failure(error: &str) {
    let _ = writeln!(
        std::io::stderr().lock(),
        "mcp-console: transcript recording disabled: {error}"
    );
}

fn report_projection_failure(error: &str) {
    let _ = writeln!(
        std::io::stderr().lock(),
        "mcp-console: transcript projections disabled: {error}"
    );
}

fn create_private_directory(path: &Path, recursive: bool) -> std::io::Result<()> {
    let mut builder = DirBuilder::new();
    builder.recursive(recursive);
    #[cfg(unix)]
    builder.mode(0o700);
    builder.create(path)
}

fn create_private_file(path: &Path) -> std::io::Result<File> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    options.open(path)
}

fn write_new(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let mut file = create_private_file(path)
        .map_err(|error| format!("failed to create {}: {error}", path.display()))?;
    file.write_all(bytes)
        .and_then(|()| file.flush())
        .map_err(|error| format!("failed to write {}: {error}", path.display()))
}

fn replace_private_file(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let temporary = path.with_file_name(".transcript.qmd.tmp");
    if let Err(error) = write_new(&temporary, bytes) {
        let _ = std::fs::remove_file(&temporary);
        return Err(error);
    }
    if let Err(error) = std::fs::rename(&temporary, path) {
        let _ = std::fs::remove_file(&temporary);
        return Err(format!(
            "failed to replace {} from {}: {error}",
            path.display(),
            temporary.display()
        ));
    }
    Ok(())
}
