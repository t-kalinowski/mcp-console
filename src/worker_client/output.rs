use std::sync::mpsc::SyncSender;
use std::sync::{Arc, Mutex};

mod preview;
mod terminal;
use preview::{Part, Preview, Source};

/// Maximum UTF-8 text and raw direct-output bytes retained between drains.
const MAX_PENDING_TEXT_BYTES: usize = 8 * 1024 * 1024;
/// Maximum encoded image payload bytes retained between drains.
const MAX_PENDING_IMAGE_BYTES: usize = 8 * 1024 * 1024;
/// Maximum image MIME-type bytes retained between drains.
const MAX_PENDING_IMAGE_METADATA_BYTES: usize = 64 * 1024;
/// Maximum ordinary text, direct-output, and image events retained between drains.
const MAX_PENDING_OUTPUT_EVENTS: usize = 4_096;

pub(super) const WORKER_STARTING_NOTICE: &str = "starting new worker";
const WORKER_STARTING_STATE: &str = "worker starting";
pub(super) const WORKER_STOPPED_NOTICE: &str = "worker stopped: in-memory state lost";
pub(super) const WORKER_IDLE_NOTICE: &str = "idle";
pub(super) const EVALUATION_STOPPED_BY_RESTART_NOTICE: &str =
    "stopped by session restart request before evaluation finished";
pub(super) const ACTIVE_EVALUATION_STOPPED_NOTICE: &str =
    "active evaluation stopped by session restart request";

/// Stores pending session output in publication order until one response drains it.
#[derive(Clone)]
pub(super) struct OutputTape(Arc<Mutex<OutputTapeState>>);

struct OutputTapeState {
    direct_stdout: DirectDecoder,
    direct_stderr: DirectDecoder,
    cell_output: Option<crate::transcript::CellOutput>,
    raw_bytes: u64,
    next_event: u64,
    events: Vec<(u64, OutputEvent)>,
    /// A failed pre-evaluation response reclaimed after unsuccessful MCP delivery.
    recovered: Option<Response>,
    limits: OutputLimits,
    budget: PendingOutputBudget,
    /// The truncation summary still open for observations before the next cut.
    active_truncation: Option<u64>,
}

#[derive(Clone, Copy)]
struct OutputLimits {
    text_bytes: usize,
    image_bytes: usize,
    image_metadata_bytes: usize,
    events: usize,
}

impl Default for OutputLimits {
    fn default() -> Self {
        Self {
            text_bytes: MAX_PENDING_TEXT_BYTES,
            image_bytes: MAX_PENDING_IMAGE_BYTES,
            image_metadata_bytes: MAX_PENDING_IMAGE_METADATA_BYTES,
            events: MAX_PENDING_OUTPUT_EVENTS,
        }
    }
}

#[derive(Default)]
struct PendingOutputBudget {
    text_bytes: usize,
    image_bytes: usize,
    image_metadata_bytes: usize,
    events: usize,
    dropping_ordinary_output: bool,
}

#[derive(Default)]
struct DirectDecoder {
    bytes: Vec<u8>,
    /// Position of the first event contributing to the incomplete scalar.
    origin: Option<u64>,
}

/// An opaque position whose only meaning is "all events published before here."
#[derive(Clone, Copy)]
pub(super) struct OutputCut(u64);

/// One publication from a directly captured worker file descriptor.
///
/// These paths capture output that bypasses worker console-text frames, including
/// Python `.buffer` writes, native fd writes, forked or execed descendants, and
/// custom workers.
enum DirectOutputEvent {
    Bytes(Vec<u8>),
    Closed,
}

enum OutputEvent {
    /// Raw bytes or closure from the worker's directly captured stdout (fd 1).
    DirectStdout(DirectOutputEvent),
    /// Raw bytes or closure from the worker's directly captured stderr (fd 2).
    DirectStderr(DirectOutputEvent),
    /// Text from a worker console-text sideband frame.
    WorkerConsoleText {
        channel: crate::worker_protocol::ConsoleChannel,
        text: Box<str>,
    },
    /// An image from a worker `image` sideband frame, already persisted when enabled.
    WorkerImage {
        data: Box<str>,
        mime_type: Box<str>,
        artifact: Option<crate::transcript::Artifact>,
    },
    /// A server-owned lifecycle or input notice, terminated before later output.
    ServerNotice(String),
    /// A server-owned informational notice retained atomically within output limits.
    BoundedServerNotice(Box<str>),
    /// A server infrastructure, transport, or protocol failure.
    ///
    /// Language errors are normal evaluation output and do not use this event.
    ServerFailure(SendFailure),
    /// One bounded summary for ordinary payload discarded in this cut segment.
    Truncated(Truncation),
    /// Raw-file ownership captured before completion releases the writer.
    Source { source: Source, close_streams: bool },
}

#[derive(Default)]
struct Truncation {
    text_bytes: usize,
    image_bytes: usize,
    image_metadata_bytes: usize,
    events: usize,
    output_path: Option<Box<str>>,
    retained_text_bytes: usize,
}

pub(super) struct DirectOutput {
    output: OutputTape,
    stream: DirectOutputStream,
}

#[derive(Clone, Copy)]
enum DirectOutputStream {
    Stdout,
    Stderr,
}

#[derive(Default)]
pub(crate) struct Response {
    preview: Box<Preview>,
    is_error: bool,
    delivery: Option<ResponseDeliveryTarget>,
}

pub(super) enum ResponseAcknowledgment {
    Delivered,
    Unclaimed(Response),
}

enum ResponseDeliveryTarget {
    Evaluation(SyncSender<ResponseAcknowledgment>),
    Output(OutputTape),
}

/// Reports whether an assembled console response reached the MCP transport.
///
/// A bounded recovery copy remains owned here after MCP projection so transport
/// cancellation or write failure can return the complete reply to restart.
pub(crate) struct ResponseDelivery {
    target: Option<ResponseDeliveryTarget>,
    unclaimed: Option<Response>,
}

#[derive(Clone)]
pub(crate) enum Content {
    Text(String),
    Image {
        data: String,
        mime_type: String,
        artifact: Option<crate::transcript::Artifact>,
    },
}

/// Constructs response regions and control state before bounded MCP projection.
#[derive(Default)]
pub(super) struct ResponseBuilder {
    response: Response,
}

#[derive(Clone, Copy)]
pub(super) enum TerminalState {
    Completed,
    Running,
    StdinNeeded,
    Idle,
    WorkerStarting,
    ReplacementReady,
}

pub(super) enum SendResponse {
    Idle(Response),
    Failed(Response),
    Running(Response),
    InputRequested(Response),
    Completed(Response),
    ReplacementStarting(Response),
    ReplacementReady(Response),
    Restarted(Response),
}

pub(super) struct SendFailure {
    pub(super) message: String,
    pub(super) worker_stopped: bool,
    preceded_restart: bool,
    worker_outcome: Option<super::WorkerProcessOutcome>,
}

impl From<String> for SendFailure {
    fn from(message: String) -> Self {
        Self {
            message,
            worker_stopped: false,
            preceded_restart: false,
            worker_outcome: None,
        }
    }
}

impl SendFailure {
    pub(super) fn worker_stopped(mut self) -> Self {
        self.worker_stopped = true;
        self
    }

    pub(super) fn worker_outcome(mut self, outcome: Option<super::WorkerProcessOutcome>) -> Self {
        self.worker_outcome = outcome;
        self
    }

    pub(super) fn preceded_restart(mut self) -> Self {
        self.preceded_restart = true;
        self
    }

    pub(super) fn should_survive_restart(&self) -> bool {
        self.worker_stopped || self.preceded_restart
    }
}

impl Response {
    pub(crate) fn tool_error(message: String) -> Self {
        let mut response = Self::default();
        response.push_tool_error(message);
        response
    }

    pub(crate) fn persist_images(
        &mut self,
        transcript: &crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<(), String> {
        for part in &mut self.preview.parts {
            let Part::Image(content) = part else {
                continue;
            };
            let Content::Image {
                data,
                mime_type,
                artifact,
            } = content
            else {
                continue;
            };
            if artifact.is_none() {
                *artifact = transcript.persist_image(call_id, data, mime_type)?;
            }
        }
        Ok(())
    }

    /// Consumes the response for the MCP adapter.
    pub(crate) fn into_parts(mut self) -> (Vec<Content>, bool, Option<ResponseDelivery>) {
        let content = self.preview.render();
        let is_error = self.is_error;
        let delivery = self.delivery.take().map(|target| ResponseDelivery {
            target: Some(target),
            unclaimed: Some(Response {
                preview: self.preview.clone(),
                is_error,
                delivery: None,
            }),
        });
        (content, is_error, delivery)
    }

    pub(super) fn extend(&mut self, mut other: Self) {
        self.with_builder(|builder| builder.append_response(&mut other));
    }

    /// Appends another logical response region without inserting a server notice.
    pub(super) fn extend_logical_region(&mut self, other: Self) {
        self.with_builder(|builder| builder.append_logical_region(other));
    }

    /// Appends cell output after this response's owned idle prelude.
    ///
    /// The canonical builder inserts the separator only when both regions are
    /// nonempty, preserving images and their order on either side.
    pub(super) fn extend_cell_after_idle_prelude(&mut self, other: Self) {
        self.with_builder(|builder| builder.append_cell_after_idle_prelude(other));
    }

    pub(super) fn acknowledge_with(&mut self, acknowledgment: SyncSender<ResponseAcknowledgment>) {
        assert!(
            self.delivery.is_none(),
            "a response can carry only one acknowledgment"
        );
        self.delivery = Some(ResponseDeliveryTarget::Evaluation(acknowledgment));
    }

    pub(super) fn recover_to(&mut self, output: OutputTape) {
        assert!(
            self.delivery.is_none(),
            "a response can carry only one delivery target"
        );
        self.delivery = Some(ResponseDeliveryTarget::Output(output));
    }

    fn is_empty(&self) -> bool {
        self.preview.is_empty()
    }

    fn is_error(&self) -> bool {
        self.is_error
    }

    pub(super) fn push_notice(&mut self, message: impl Into<String>) {
        self.with_builder(|builder| builder.notice(message));
    }

    /// Adds a server notice and ends its line for any output appended later.
    pub(super) fn push_notice_line(&mut self, message: impl Into<String>) {
        self.with_builder(|builder| builder.notice_line(message));
    }

    pub(super) fn push_tool_error(&mut self, message: impl Into<String>) {
        self.with_builder(|builder| builder.tool_error(message));
    }

    pub(super) fn push_failure(&mut self, failure: SendFailure) {
        self.with_builder(|builder| builder.send_failure(failure));
    }

    pub(super) fn mark_error(&mut self) {
        self.with_builder(ResponseBuilder::mark_error);
    }

    fn with_builder(&mut self, operation: impl FnOnce(&mut ResponseBuilder)) {
        let mut builder = ResponseBuilder::from_response(std::mem::take(self));
        operation(&mut builder);
        *self = builder.finish();
    }
}

impl ResponseBuilder {
    fn truncation(&mut self, truncated: Truncation) {
        let event = if truncated.events == 1 {
            "event"
        } else {
            "events"
        };
        let mut message = if truncated.image_metadata_bytes == 0 {
            format!(
                "collector limit: omitted {} text bytes and {} encoded image bytes across {} {event}",
                truncated.text_bytes, truncated.image_bytes, truncated.events
            )
        } else {
            format!(
                "collector limit: omitted {} text bytes, {} encoded image bytes, and {} image metadata bytes across {} {event}",
                truncated.text_bytes,
                truncated.image_bytes,
                truncated.image_metadata_bytes,
                truncated.events
            )
        };
        if let Some(path) = truncated.output_path {
            message.push_str(&format!(
                "; retained text: {path} ({} of {} omitted text bytes)",
                truncated.retained_text_bytes, truncated.text_bytes
            ));
        }
        self.notice(message);
    }

    fn text(&mut self, text: impl AsRef<str>) {
        self.response.preview.text(text.as_ref());
    }

    pub(super) fn new() -> Self {
        Self::default()
    }

    pub(super) fn from_response(response: Response) -> Self {
        Self { response }
    }

    pub(super) fn finish(self) -> Response {
        self.response
    }

    pub(super) fn image(
        &mut self,
        data: String,
        mime_type: String,
        artifact: Option<crate::transcript::Artifact>,
    ) {
        self.response.preview.image(Content::Image {
            data,
            mime_type,
            artifact,
        });
    }

    pub(super) fn append_response(&mut self, other: &mut Response) {
        if other.delivery.is_some() {
            assert!(
                self.response.delivery.is_none(),
                "a response can carry only one delivery target"
            );
            self.response.delivery = other.delivery.take();
        }
        self.response
            .preview
            .extend(*std::mem::take(&mut other.preview));
        self.response.is_error |= other.is_error;
    }

    pub(super) fn append_logical_region(&mut self, mut other: Response) {
        if matches!(
            self.response.preview.last_visible(),
            Some(Part::Text(_) | Part::Notice(_))
        ) && !self.response.preview.ends_with_newline()
            && other.preview.starts_with_text()
        {
            self.response.preview.notice("\n".to_owned());
        }
        self.append_response(&mut other);
    }

    pub(super) fn append_cell_after_idle_prelude(&mut self, mut cell: Response) {
        if !self.response.is_empty() && !cell.is_empty() {
            self.notice_line("output produced while idle");
        }
        self.append_response(&mut cell);
    }

    pub(super) fn notice(&mut self, message: impl Into<String>) {
        self.control_text(render_notice(message));
    }

    pub(super) fn notice_line(&mut self, message: impl Into<String>) {
        self.control_text(format!("{}\n", render_notice(message)));
    }

    fn control_text(&mut self, text: String) {
        let prefix = if !self.response.is_empty() && self.needs_line_break() {
            "\n"
        } else {
            ""
        };
        self.response.preview.notice(format!("{prefix}{text}"));
    }

    pub(super) fn server_failure(&mut self, message: impl Into<String>) {
        self.notice(message);
        self.mark_error();
    }

    fn send_failure(&mut self, failure: SendFailure) {
        self.server_failure(failure.message);
        if let Some(outcome) = failure.worker_outcome {
            self.notice(outcome.diagnostic());
        }
        if failure.worker_stopped {
            self.notice(WORKER_STOPPED_NOTICE);
        }
    }

    pub(super) fn tool_error(&mut self, message: impl Into<String>) {
        self.control_text(message.into());
        self.mark_error();
    }

    pub(super) fn terminal(&mut self, state: TerminalState) {
        match state {
            TerminalState::Completed => {
                if self.response.is_empty() {
                    self.notice("done");
                }
            }
            TerminalState::Running => self.state_banner("running; poll with an empty send"),
            TerminalState::StdinNeeded => {
                let prefix = if self.needs_line_break() { "\n" } else { "" };
                self.response
                    .preview
                    .notice(format!("{prefix}{}", render_notice("waiting for stdin")));
            }
            TerminalState::Idle => {
                if !self.response.is_error() {
                    self.state_banner(WORKER_IDLE_NOTICE);
                }
            }
            TerminalState::WorkerStarting => self.notice(WORKER_STARTING_STATE),
            TerminalState::ReplacementReady => self.notice(WORKER_IDLE_NOTICE),
        }
    }

    pub(super) fn mark_error(&mut self) {
        self.response.is_error = true;
    }

    fn state_banner(&mut self, state: &str) {
        self.response
            .preview
            .notice(format!("\n{}", render_notice(state)));
    }

    fn needs_line_break(&self) -> bool {
        !self.response.preview.ends_with_newline()
    }
}

impl ResponseDelivery {
    pub(crate) fn delivered(mut self) {
        self.unclaimed = None;
        if let Some(target) = self.target.take() {
            target.delivered();
        }
    }

    pub(crate) fn unclaimed(mut self) {
        self.return_unclaimed();
    }

    fn return_unclaimed(&mut self) {
        let Some(target) = self.target.take() else {
            return;
        };
        let response = self
            .unclaimed
            .take()
            .expect("response delivery with a target must retain its reply");
        target.unclaimed(response);
    }
}

impl Drop for ResponseDelivery {
    fn drop(&mut self) {
        self.return_unclaimed();
    }
}

impl Drop for Response {
    fn drop(&mut self) {
        let Some(target) = self.delivery.take() else {
            return;
        };
        let response = Self {
            preview: std::mem::take(&mut self.preview),
            is_error: self.is_error,
            delivery: None,
        };
        target.unclaimed(response);
    }
}

impl ResponseDeliveryTarget {
    fn delivered(self) {
        if let Self::Evaluation(acknowledgment) = self {
            let _ = acknowledgment.send(ResponseAcknowledgment::Delivered);
        }
    }

    fn unclaimed(self, response: Response) {
        match self {
            Self::Evaluation(acknowledgment) => {
                let _ = acknowledgment.send(ResponseAcknowledgment::Unclaimed(response));
            }
            Self::Output(output) => output.recover(response),
        }
    }
}

impl OutputTape {
    pub(super) fn new() -> Self {
        Self::with_limits(OutputLimits::default())
    }

    fn with_limits(limits: OutputLimits) -> Self {
        Self(Arc::new(Mutex::new(OutputTapeState::new(limits))))
    }

    fn recover(&self, response: Response) {
        let mut state = self.lock();
        assert!(
            state.recovered.is_none(),
            "the output tape can recover only one response at a time"
        );
        state.recovered = Some(response);
    }

    pub(super) fn direct_stdout(&self) -> DirectOutput {
        self.direct_output(DirectOutputStream::Stdout)
    }

    pub(super) fn direct_stderr(&self) -> DirectOutput {
        self.direct_output(DirectOutputStream::Stderr)
    }

    fn direct_output(&self, stream: DirectOutputStream) -> DirectOutput {
        DirectOutput {
            output: self.clone(),
            stream,
        }
    }

    pub(super) fn push_console_text(
        &self,
        channel: crate::worker_protocol::ConsoleChannel,
        text: impl Into<String>,
    ) {
        let text = text.into();
        if !text.is_empty() {
            self.lock().push_console_text(channel, text);
        }
    }

    pub(super) fn push_image(
        &self,
        data: String,
        mime_type: String,
        artifact: Option<crate::transcript::Artifact>,
    ) {
        self.push_image_with_artifact(data, mime_type, move |_, _| Ok(artifact))
            .expect("infallible image artifact closure failed");
    }

    /// Admits an image before creating its transcript artifact.
    ///
    /// The closure runs only when the complete encoded payload fits. It runs
    /// while the tape is locked so direct-output readers cannot overtake the
    /// image between admission and publication.
    pub(super) fn push_image_with_artifact<F>(
        &self,
        data: String,
        mime_type: String,
        make_artifact: F,
    ) -> Result<(), String>
    where
        F: FnOnce(&str, &str) -> Result<Option<crate::transcript::Artifact>, String>,
    {
        self.lock()
            .push_image_with_artifact(data, mime_type, make_artifact)
    }

    /// Publishes a server notice that ends its line before later worker output.
    pub(super) fn push_notice_line(&self, message: impl Into<String>) {
        self.lock()
            .push_control(OutputEvent::ServerNotice(message.into()));
    }

    /// Publishes a bounded server notice, retaining either the complete line or none of it.
    pub(super) fn push_bounded_notice_line(&self, message: impl Into<String>) {
        self.lock().push_bounded_notice_line(message.into());
    }

    pub(super) fn push_failure(&self, failure: SendFailure) {
        self.lock()
            .push_control(OutputEvent::ServerFailure(failure));
    }

    pub(super) fn cut(&self) -> OutputCut {
        let mut state = self.lock();
        state.flush_cell_output();
        state.seal_interval(false);
        OutputCut(state.next_event)
    }

    pub(super) fn take(&self) -> Response {
        let mut state = self.lock();
        state.flush_cell_output();
        state.seal_interval(false);
        let cut = OutputCut(state.next_event);
        drain_through(&mut state, cut, false)
    }

    /// Transfers all currently pending output into an evaluation-owned prelude.
    ///
    /// Unlike an ordinary poll, this closes incomplete direct-stream UTF-8 at
    /// the ownership boundary so later cell bytes cannot complete idle output.
    pub(super) fn take_prelude(&self) -> Response {
        self.take_prelude_before(|| {})
    }

    /// Transfers a prelude and establishes an admission boundary while the
    /// tape remains locked.
    pub(super) fn take_prelude_before(&self, boundary: impl FnOnce()) -> Response {
        let mut state = self.lock();
        state.seal_interval(true);
        let cut = OutputCut(state.next_event);
        let response = drain_through(&mut state, cut, true);
        boundary();
        response
    }

    /// Establishes one cell's output file at the same boundary as its worker operation.
    pub(super) fn begin_cell_output_before(
        &self,
        cell_output: Option<crate::transcript::CellOutput>,
        capture_prelude: bool,
        boundary: impl FnOnce(),
    ) -> Response {
        let mut state = self.lock();
        assert!(
            state.cell_output.is_none(),
            "only one cell output file can be active"
        );
        state.seal_interval(true);
        let response = if capture_prelude {
            let cut = OutputCut(state.next_event);
            drain_through(&mut state, cut, true)
        } else {
            Response::default()
        };
        state.cell_output = cell_output;
        boundary();
        response
    }

    /// Finishes one cell's file at the same ordered boundary as its completion cut.
    pub(super) fn finish_cell_output(&self) -> OutputCut {
        let mut state = self.lock();
        state.seal_interval(true);
        state.finish_cell_output();
        OutputCut(state.next_event)
    }

    pub(super) fn drain_through(&self, cut: OutputCut) -> Response {
        let mut state = self.lock();
        drain_through(&mut state, cut, false)
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, OutputTapeState> {
        self.0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl DirectOutput {
    pub(super) fn push(&self, bytes: &[u8]) {
        self.output.lock().push_direct_output(self.stream, bytes);
    }

    pub(super) fn close(&self) {
        self.output
            .lock()
            .push_control(self.stream.event(DirectOutputEvent::Closed));
    }
}

impl DirectOutputStream {
    fn event(self, event: DirectOutputEvent) -> OutputEvent {
        match self {
            Self::Stdout => OutputEvent::DirectStdout(event),
            Self::Stderr => OutputEvent::DirectStderr(event),
        }
    }
}

impl OutputTapeState {
    fn new(limits: OutputLimits) -> Self {
        Self {
            direct_stdout: DirectDecoder::default(),
            direct_stderr: DirectDecoder::default(),
            cell_output: None,
            raw_bytes: 0,
            next_event: 0,
            events: Vec::new(),
            recovered: None,
            limits,
            budget: PendingOutputBudget::default(),
            active_truncation: None,
        }
    }

    fn flush_cell_output(&mut self) {
        let notice = self.cell_output.as_mut().and_then(|output| output.flush());
        self.push_cell_output_notice(notice);
    }

    fn finish_cell_output(&mut self) {
        let notice = self.cell_output.take().and_then(|output| output.finish());
        self.push_cell_output_notice(notice);
    }

    fn push_console_text(&mut self, channel: crate::worker_protocol::ConsoleChannel, text: String) {
        self.raw_bytes += text.len() as u64;
        let original_length = text.len();
        let (spooled, cell_output_notice) = self
            .cell_output
            .as_mut()
            .map(|output| output.append(text.as_bytes()))
            .unwrap_or_default();
        if self.budget.dropping_ordinary_output {
            self.omit_cell_text(original_length, spooled);
            self.push_cell_output_notice(cell_output_notice);
            return;
        }

        let remaining = self
            .limits
            .text_bytes
            .saturating_sub(self.budget.text_bytes);
        let retained = if self.budget.events < self.limits.events {
            utf8_prefix_length(&text, remaining)
        } else {
            0
        };
        if retained > 0 {
            let retained_text = if retained == original_length {
                text.into_boxed_str()
            } else {
                Box::<str>::from(&text[..retained])
            };
            self.retain_ordinary(
                OutputEvent::WorkerConsoleText {
                    channel,
                    text: retained_text,
                },
                retained,
                0,
                0,
            );
        }
        if retained < original_length {
            self.omit_cell_text(original_length - retained, spooled.saturating_sub(retained));
        }
        self.push_cell_output_notice(cell_output_notice);
    }

    fn push_bounded_notice_line(&mut self, message: String) {
        // Include brackets, the trailing newline, and a possible leading delimiter.
        let rendered_length = message.len().saturating_add(4);
        let fits = !self.budget.dropping_ordinary_output
            && self.budget.events < self.limits.events
            && rendered_length
                <= self
                    .limits
                    .text_bytes
                    .saturating_sub(self.budget.text_bytes);
        if fits {
            self.retain_ordinary(
                OutputEvent::BoundedServerNotice(message.into_boxed_str()),
                rendered_length,
                0,
                0,
            );
        } else {
            self.omit(rendered_length, 0, 0, 1, None, 0);
        }
    }

    fn push_image_with_artifact<F>(
        &mut self,
        data: String,
        mime_type: String,
        make_artifact: F,
    ) -> Result<(), String>
    where
        F: FnOnce(&str, &str) -> Result<Option<crate::transcript::Artifact>, String>,
    {
        let length = data.len();
        let metadata_length = mime_type.len();
        let fits = !self.budget.dropping_ordinary_output
            && self.budget.events < self.limits.events
            && length
                <= self
                    .limits
                    .image_bytes
                    .saturating_sub(self.budget.image_bytes)
            && metadata_length
                <= self
                    .limits
                    .image_metadata_bytes
                    .saturating_sub(self.budget.image_metadata_bytes);
        if fits {
            let artifact = make_artifact(&data, &mime_type)?;
            self.retain_ordinary(
                OutputEvent::WorkerImage {
                    data: data.into_boxed_str(),
                    mime_type: mime_type.into_boxed_str(),
                    artifact,
                },
                0,
                length,
                metadata_length,
            );
        } else {
            self.omit(0, length, metadata_length, 1, None, 0);
        }
        Ok(())
    }

    fn push_direct_output(&mut self, stream: DirectOutputStream, bytes: &[u8]) {
        self.raw_bytes += bytes.len() as u64;
        if bytes.is_empty() {
            return;
        }
        let (spooled, cell_output_notice) = self
            .cell_output
            .as_mut()
            .map(|output| output.append(bytes))
            .unwrap_or_default();
        if self.budget.dropping_ordinary_output {
            self.omit_cell_text(bytes.len(), spooled);
            self.push_cell_output_notice(cell_output_notice);
            return;
        }

        let retained = if self.budget.events < self.limits.events {
            bytes.len().min(
                self.limits
                    .text_bytes
                    .saturating_sub(self.budget.text_bytes),
            )
        } else {
            0
        };
        if retained > 0 {
            self.retain_ordinary(
                stream.event(DirectOutputEvent::Bytes(bytes[..retained].to_vec())),
                retained,
                0,
                0,
            );
        }
        if retained < bytes.len() {
            self.omit_cell_text(bytes.len() - retained, spooled.saturating_sub(retained));
        }
        self.push_cell_output_notice(cell_output_notice);
    }

    fn retain_ordinary(
        &mut self,
        event: OutputEvent,
        text_bytes: usize,
        image_bytes: usize,
        image_metadata_bytes: usize,
    ) {
        self.budget.text_bytes = self.budget.text_bytes.saturating_add(text_bytes);
        self.budget.image_bytes = self.budget.image_bytes.saturating_add(image_bytes);
        self.budget.image_metadata_bytes = self
            .budget
            .image_metadata_bytes
            .saturating_add(image_metadata_bytes);
        self.budget.events = self.budget.events.saturating_add(1);
        self.push_event(event);
    }

    fn push_control(&mut self, event: OutputEvent) {
        self.push_event(event);
    }

    fn push_event(&mut self, event: OutputEvent) -> u64 {
        let sequence = self.allocate_position();
        self.events.push((sequence, event));
        sequence
    }

    fn omit(
        &mut self,
        text_bytes: usize,
        image_bytes: usize,
        image_metadata_bytes: usize,
        events: usize,
        output_path: Option<Box<str>>,
        retained_text_bytes: usize,
    ) {
        self.budget.dropping_ordinary_output = true;
        if let Some(sequence) = self.active_truncation {
            let index = self
                .events
                .binary_search_by_key(&sequence, |(sequence, _)| *sequence)
                .expect("active output truncation must remain on the tape");
            let OutputEvent::Truncated(truncation) = &mut self.events[index].1 else {
                unreachable!("active output truncation points at another event")
            };
            truncation.text_bytes = truncation.text_bytes.saturating_add(text_bytes);
            truncation.image_bytes = truncation.image_bytes.saturating_add(image_bytes);
            truncation.image_metadata_bytes = truncation
                .image_metadata_bytes
                .saturating_add(image_metadata_bytes);
            truncation.events = truncation.events.saturating_add(events);
            truncation.retained_text_bytes = truncation
                .retained_text_bytes
                .saturating_add(retained_text_bytes);
            if truncation.output_path.is_none() {
                truncation.output_path = output_path;
            }
            // Omitted publications still occupy observation-order positions, so a
            // later cut can seal counts without retaining one event per chunk.
            self.allocate_position();
        } else {
            let sequence = self.push_event(OutputEvent::Truncated(Truncation {
                text_bytes,
                image_bytes,
                image_metadata_bytes,
                events,
                output_path,
                retained_text_bytes,
            }));
            self.active_truncation = Some(sequence);
        }
    }

    fn omit_cell_text(&mut self, text_bytes: usize, retained_text_bytes: usize) {
        let output_path = self.cell_output.as_mut().and_then(|output| {
            (retained_text_bytes > 0).then(|| Box::<str>::from(output.record().public_path()))
        });
        self.omit(text_bytes, 0, 0, 1, output_path, retained_text_bytes);
    }

    fn push_cell_output_notice(&mut self, notice: Option<String>) {
        if let Some(notice) = notice {
            self.push_control(OutputEvent::ServerNotice(notice));
        }
    }

    fn allocate_position(&mut self) -> u64 {
        let position = self.next_event;
        self.next_event = self
            .next_event
            .checked_add(1)
            .expect("output tape position overflowed");
        position
    }

    fn seal_interval(&mut self, close_streams: bool) {
        self.active_truncation = None;
        let source = match &self.cell_output {
            Some(output) => Source {
                file: Some(output.record()),
                raw_bytes: self.raw_bytes,
                retained_bytes: output.retained_bytes(),
                discarded_bytes: output.discarded_bytes(),
            },
            None => Source {
                raw_bytes: self.raw_bytes,
                ..Source::default()
            },
        };
        self.push_control(OutputEvent::Source {
            source,
            close_streams,
        });
        self.raw_bytes = 0;
    }

    fn recompute_budget(&mut self) {
        let mut budget = PendingOutputBudget {
            text_bytes: self
                .direct_stdout
                .bytes
                .len()
                .saturating_add(self.direct_stderr.bytes.len()),
            ..PendingOutputBudget::default()
        };
        for (_, event) in &self.events {
            match event {
                OutputEvent::DirectStdout(DirectOutputEvent::Bytes(bytes))
                | OutputEvent::DirectStderr(DirectOutputEvent::Bytes(bytes)) => {
                    budget.text_bytes = budget.text_bytes.saturating_add(bytes.len());
                    budget.events = budget.events.saturating_add(1);
                }
                OutputEvent::WorkerConsoleText { text, .. } => {
                    budget.text_bytes = budget.text_bytes.saturating_add(text.len());
                    budget.events = budget.events.saturating_add(1);
                }
                OutputEvent::BoundedServerNotice(message) => {
                    budget.text_bytes = budget
                        .text_bytes
                        .saturating_add(message.len().saturating_add(4));
                    budget.events = budget.events.saturating_add(1);
                }
                OutputEvent::WorkerImage {
                    data, mime_type, ..
                } => {
                    budget.image_bytes = budget.image_bytes.saturating_add(data.len());
                    budget.image_metadata_bytes =
                        budget.image_metadata_bytes.saturating_add(mime_type.len());
                    budget.events = budget.events.saturating_add(1);
                }
                OutputEvent::Truncated(_) => budget.dropping_ordinary_output = true,
                OutputEvent::DirectStdout(DirectOutputEvent::Closed)
                | OutputEvent::DirectStderr(DirectOutputEvent::Closed)
                | OutputEvent::ServerNotice(_)
                | OutputEvent::ServerFailure(_)
                | OutputEvent::Source { .. } => {}
            }
        }
        if self.active_truncation.is_some_and(|sequence| {
            self.events
                .binary_search_by_key(&sequence, |(sequence, _)| *sequence)
                .is_err()
        }) {
            self.active_truncation = None;
        }
        self.budget = budget;
    }
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum LogicalStream {
    ConsoleOutput,
    ConsoleDiagnostic,
    DirectStdout,
    DirectStderr,
}

#[derive(Default)]
struct SegmentCompactor {
    stream: Option<LogicalStream>,
    terminal: terminal::Stream,
}

impl SegmentCompactor {
    fn text(&mut self, output: &mut ResponseBuilder, stream: LogicalStream, text: &str) {
        if text.is_empty() {
            return;
        }
        if self.stream != Some(stream) {
            self.flush(output);
            self.stream = Some(stream);
        }
        output.text(self.terminal.ingest(text));
    }

    fn flush(&mut self, output: &mut ResponseBuilder) {
        if self.stream.take().is_some() {
            output.text(self.terminal.finish());
        }
    }
}

fn drain_through(
    state: &mut OutputTapeState,
    cut: OutputCut,
    flush_direct_decoders: bool,
) -> Response {
    let boundary = state
        .events
        .partition_point(|(sequence, _)| *sequence < cut.0);
    let remaining = state.events.split_off(boundary);
    let events = std::mem::replace(&mut state.events, remaining);
    let mut output = ResponseBuilder::from_response(state.recovered.take().unwrap_or_default());
    let mut compactor = SegmentCompactor::default();

    for (sequence, event) in events {
        match &event {
            OutputEvent::Source {
                close_streams: false,
                ..
            } => {}
            OutputEvent::DirectStdout(DirectOutputEvent::Bytes(_)) => {
                flush_direct_decoder_before(
                    &mut output,
                    &mut compactor,
                    &mut state.direct_stderr,
                    LogicalStream::DirectStderr,
                    sequence,
                );
            }
            OutputEvent::DirectStderr(DirectOutputEvent::Bytes(_)) => {
                flush_direct_decoder_before(
                    &mut output,
                    &mut compactor,
                    &mut state.direct_stdout,
                    LogicalStream::DirectStdout,
                    sequence,
                );
            }
            _ => flush_direct_decoders_before(
                &mut output,
                &mut compactor,
                &mut state.direct_stdout,
                &mut state.direct_stderr,
                sequence,
            ),
        }
        match event {
            OutputEvent::DirectStdout(event) => append_direct_output(
                &mut output,
                &mut compactor,
                &mut state.direct_stdout,
                LogicalStream::DirectStdout,
                sequence,
                event,
            ),
            OutputEvent::DirectStderr(event) => append_direct_output(
                &mut output,
                &mut compactor,
                &mut state.direct_stderr,
                LogicalStream::DirectStderr,
                sequence,
                event,
            ),
            OutputEvent::WorkerConsoleText { channel, text } => {
                let stream = match channel {
                    crate::worker_protocol::ConsoleChannel::Output => LogicalStream::ConsoleOutput,
                    crate::worker_protocol::ConsoleChannel::Diagnostic => {
                        LogicalStream::ConsoleDiagnostic
                    }
                };
                compactor.text(&mut output, stream, &text);
            }
            OutputEvent::WorkerImage {
                data,
                mime_type,
                artifact,
            } => {
                compactor.flush(&mut output);
                output.image(data.into_string(), mime_type.into_string(), artifact);
            }
            OutputEvent::ServerNotice(message) => {
                compactor.flush(&mut output);
                output.notice_line(message);
            }
            OutputEvent::BoundedServerNotice(message) => {
                compactor.flush(&mut output);
                let prefix = if output.needs_line_break() && !output.response.is_empty() {
                    "\n"
                } else {
                    ""
                };
                output
                    .response
                    .preview
                    .information(format!("{prefix}[{message}]\n"));
            }
            OutputEvent::ServerFailure(failure) => {
                compactor.flush(&mut output);
                output.send_failure(failure);
            }
            OutputEvent::Source { source, .. } => {
                compactor.flush(&mut output);
                output.response.preview.source(source);
            }
            OutputEvent::Truncated(truncated) => {
                compactor.flush(&mut output);
                output.truncation(truncated);
            }
        }
    }

    if flush_direct_decoders {
        flush_direct_decoders_before(
            &mut output,
            &mut compactor,
            &mut state.direct_stdout,
            &mut state.direct_stderr,
            cut.0,
        );
    }
    compactor.flush(&mut output);
    state.recompute_budget();
    output.finish()
}

fn append_direct_output(
    output: &mut ResponseBuilder,
    compactor: &mut SegmentCompactor,
    pending: &mut DirectDecoder,
    stream: LogicalStream,
    sequence: u64,
    event: DirectOutputEvent,
) {
    match event {
        DirectOutputEvent::Bytes(bytes) => {
            let prior_origin = pending.origin.unwrap_or(sequence);
            pending.bytes.extend_from_slice(&bytes);
            let complete = complete_utf8_prefix(&pending.bytes);
            let incomplete = pending.bytes.split_off(complete);
            let complete = std::mem::replace(&mut pending.bytes, incomplete);
            compactor.text(output, stream, &String::from_utf8_lossy(&complete));
            pending.origin = if pending.bytes.is_empty() {
                None
            } else if complete.is_empty() {
                Some(prior_origin)
            } else {
                Some(sequence)
            };
        }
        DirectOutputEvent::Closed => {
            flush_direct_decoder_compacted(output, compactor, pending, stream);
            if compactor.stream == Some(stream) {
                compactor.flush(output);
            }
        }
    }
}

fn flush_direct_decoder_compacted(
    output: &mut ResponseBuilder,
    compactor: &mut SegmentCompactor,
    pending: &mut DirectDecoder,
    stream: LogicalStream,
) {
    compactor.text(output, stream, &String::from_utf8_lossy(&pending.bytes));
    pending.bytes.clear();
    pending.origin = None;
}

fn flush_direct_decoder_before(
    output: &mut ResponseBuilder,
    compactor: &mut SegmentCompactor,
    pending: &mut DirectDecoder,
    stream: LogicalStream,
    sequence: u64,
) {
    if pending.origin.is_some_and(|origin| origin < sequence) {
        flush_direct_decoder_compacted(output, compactor, pending, stream);
    }
}

fn flush_direct_decoders_before(
    output: &mut ResponseBuilder,
    compactor: &mut SegmentCompactor,
    stdout: &mut DirectDecoder,
    stderr: &mut DirectDecoder,
    sequence: u64,
) {
    let stdout_origin = stdout.origin.filter(|origin| *origin < sequence);
    let stderr_origin = stderr.origin.filter(|origin| *origin < sequence);
    if stdout_origin <= stderr_origin {
        flush_direct_decoder_before(
            output,
            compactor,
            stdout,
            LogicalStream::DirectStdout,
            sequence,
        );
        flush_direct_decoder_before(
            output,
            compactor,
            stderr,
            LogicalStream::DirectStderr,
            sequence,
        );
    } else {
        flush_direct_decoder_before(
            output,
            compactor,
            stderr,
            LogicalStream::DirectStderr,
            sequence,
        );
        flush_direct_decoder_before(
            output,
            compactor,
            stdout,
            LogicalStream::DirectStdout,
            sequence,
        );
    }
}

fn complete_utf8_prefix(bytes: &[u8]) -> usize {
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

pub(super) fn project_completed(output: Response) -> Response {
    project_terminal(output, TerminalState::Completed)
}

pub(super) fn project_controlled_completed(output: Response) -> Response {
    if output.is_error() {
        return output;
    }
    let mut builder = ResponseBuilder::from_response(output);
    builder.notice("done");
    builder.finish()
}

pub(super) fn project_replacement_ready(output: Response) -> Response {
    project_terminal(output, TerminalState::ReplacementReady)
}

pub(super) fn render_response(response: SendResponse) -> Response {
    let (output, terminal) = match response {
        SendResponse::Completed(output) => (output, Some(TerminalState::Completed)),
        SendResponse::Failed(output) | SendResponse::Restarted(output) => (output, None),
        SendResponse::InputRequested(output) => (output, Some(TerminalState::StdinNeeded)),
        SendResponse::Running(output) => (output, Some(TerminalState::Running)),
        SendResponse::Idle(output) => (output, Some(TerminalState::Idle)),
        SendResponse::ReplacementStarting(output) => (output, Some(TerminalState::WorkerStarting)),
        SendResponse::ReplacementReady(output) => (output, Some(TerminalState::ReplacementReady)),
    };
    match terminal {
        Some(terminal) => project_terminal(output, terminal),
        None => output,
    }
}

fn project_terminal(output: Response, terminal: TerminalState) -> Response {
    let mut builder = ResponseBuilder::from_response(output);
    builder.terminal(terminal);
    builder.finish()
}

pub(super) fn direct_failure(message: impl Into<String>) -> Response {
    let mut builder = ResponseBuilder::new();
    builder.server_failure(message);
    builder.finish()
}

fn render_notice(message: impl Into<String>) -> String {
    format!("[{}]", message.into())
}

fn utf8_prefix_length(text: &str, limit: usize) -> usize {
    let mut length = text.len().min(limit);
    while !text.is_char_boundary(length) {
        length -= 1;
    }
    length
}
