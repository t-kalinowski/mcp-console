use std::sync::mpsc::SyncSender;
use std::sync::{Arc, Mutex};

mod terminal;

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
    console_output: terminal::Stream,
    console_diagnostic: terminal::Stream,
    terminal_stdout: terminal::Stream,
    terminal_stderr: terminal::Stream,
    next_event: u64,
    events: Vec<(u64, OutputEvent)>,
    /// A failed pre-evaluation response reclaimed after unsuccessful MCP delivery.
    recovered: Option<Response>,
    limits: OutputLimits,
    budget: PendingOutputBudget,
    /// The truncation summary still open for observations before the next cut.
    active_truncation: Option<u64>,
    /// The truncation event that currently blocks later ordinary output.
    drop_truncation: Option<u64>,
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

#[derive(Clone, Copy, Eq, PartialEq)]
enum LogicalStream {
    ConsoleOutput,
    ConsoleDiagnostic,
    DirectStdout,
    DirectStderr,
}

enum OutputEvent {
    /// Text from one worker console channel or directly captured file descriptor.
    Text {
        text: Box<str>,
        text_bytes: usize,
        /// The suffix that may still be replaced by a redraw before the next cut.
        active_suffix: Option<ActiveSuffix>,
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
}

#[derive(Clone, Copy)]
struct ActiveSuffix {
    text_offset: usize,
    source_offset: usize,
}

#[derive(Clone, Copy)]
struct SourceText {
    bytes: usize,
    active_suffix: Option<usize>,
}

impl OutputEvent {
    fn clear_active_suffix(&mut self) {
        if let Self::Text { active_suffix, .. } = self {
            *active_suffix = None;
        }
    }
}

#[derive(Default)]
struct Truncation {
    text_bytes: usize,
    image_bytes: usize,
    image_metadata_bytes: usize,
    events: usize,
}

impl Truncation {
    fn is_empty(&self) -> bool {
        self.text_bytes == 0
            && self.image_bytes == 0
            && self.image_metadata_bytes == 0
            && self.events == 0
    }
}

#[cfg(target_os = "macos")]
pub(super) struct DirectOutput {
    output: OutputTape,
    stream: DirectOutputStream,
}

#[cfg(target_os = "macos")]
#[derive(Clone, Copy)]
enum DirectOutputStream {
    Stdout,
    Stderr,
}

#[derive(Default)]
pub(crate) struct Response {
    content: Vec<Content>,
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

/// The only implementation of public content coalescing and newline projection.
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
    pub(crate) fn persist_images(
        &mut self,
        transcript: &crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<(), String> {
        for content in &mut self.content {
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
        let is_error = self.is_error;
        let delivery = self.delivery.take().map(|target| ResponseDelivery {
            target: Some(target),
            unclaimed: Some(Response {
                content: self.content.clone(),
                is_error,
                delivery: None,
            }),
        });
        let content = std::mem::take(&mut self.content);
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
        self.content.is_empty()
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
    pub(super) fn new() -> Self {
        Self::default()
    }

    pub(super) fn from_response(response: Response) -> Self {
        Self { response }
    }

    pub(super) fn finish(self) -> Response {
        self.response
    }

    pub(super) fn text(&mut self, text: impl Into<String>) {
        let text = text.into();
        if text.is_empty() {
            return;
        }
        if let Some(Content::Text(output)) = self.response.content.last_mut() {
            output.push_str(&text);
        } else {
            self.response.content.push(Content::Text(text));
        }
    }

    pub(super) fn image(
        &mut self,
        data: String,
        mime_type: String,
        artifact: Option<crate::transcript::Artifact>,
    ) {
        self.response.content.push(Content::Image {
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
        for content in std::mem::take(&mut other.content) {
            match content {
                Content::Text(text) => self.text(text),
                Content::Image {
                    data,
                    mime_type,
                    artifact,
                } => self.image(data, mime_type, artifact),
            }
        }
        self.response.is_error |= other.is_error;
    }

    pub(super) fn append_logical_region(&mut self, mut other: Response) {
        if matches!(self.response.content.last(), Some(Content::Text(text)) if !text.ends_with('\n'))
            && matches!(other.content.first(), Some(Content::Text(_)))
        {
            self.text("\n");
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
        self.line(render_notice(message));
    }

    pub(super) fn notice_line(&mut self, message: impl Into<String>) {
        self.notice(message);
        self.text("\n");
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
        self.line(message);
        self.mark_error();
    }

    fn truncation(&mut self, truncated: Truncation) {
        let event = if truncated.events == 1 {
            "event"
        } else {
            "events"
        };
        let message = if truncated.image_metadata_bytes == 0 {
            format!(
                "output truncated: omitted {} text bytes and {} encoded image bytes across {} {event}",
                truncated.text_bytes, truncated.image_bytes, truncated.events
            )
        } else {
            format!(
                "output truncated: omitted {} text bytes, {} encoded image bytes, and {} image metadata bytes across {} {event}",
                truncated.text_bytes,
                truncated.image_bytes,
                truncated.image_metadata_bytes,
                truncated.events
            )
        };
        self.notice(message);
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
                if self.needs_line_break() {
                    self.text("\n");
                }
                self.text(render_notice("waiting for stdin"));
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

    fn line(&mut self, text: impl Into<String>) {
        if !self.response.is_empty() && self.needs_line_break() {
            self.text("\n");
        }
        self.text(text);
    }

    fn state_banner(&mut self, state: &str) {
        self.text("\n");
        self.text(render_notice(state));
    }

    fn needs_line_break(&self) -> bool {
        !matches!(
            self.response.content.last(),
            Some(Content::Text(text)) if text.ends_with('\n')
        )
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
            content: std::mem::take(&mut self.content),
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

    #[cfg(target_os = "macos")]
    pub(super) fn direct_stdout(&self) -> DirectOutput {
        self.direct_output(DirectOutputStream::Stdout)
    }

    #[cfg(target_os = "macos")]
    pub(super) fn direct_stderr(&self) -> DirectOutput {
        self.direct_output(DirectOutputStream::Stderr)
    }

    #[cfg(target_os = "macos")]
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
        state.snapshot_streams();
        state.seal_streams();
        state.seal_truncation();
        OutputCut(state.next_event)
    }

    pub(super) fn completion_cut(&self) -> OutputCut {
        let mut state = self.lock();
        state.flush_direct_decoders();
        state.finish_streams();
        state.seal_streams();
        state.seal_truncation();
        OutputCut(state.next_event)
    }

    pub(super) fn take(&self) -> Response {
        let mut state = self.lock();
        state.snapshot_streams();
        state.seal_streams();
        state.seal_truncation();
        let cut = OutputCut(state.next_event);
        drain_through(&mut state, cut)
    }

    /// Drains output from a retiring worker and clears all per-generation state.
    pub(super) fn take_generation(&self) -> Response {
        let mut state = self.lock();
        state.flush_direct_decoders();
        state.finish_streams();
        state.seal_streams();
        state.seal_truncation();
        let cut = OutputCut(state.next_event);
        drain_through(&mut state, cut)
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
        state.flush_direct_decoders();
        state.finish_streams();
        state.seal_streams();
        state.seal_truncation();
        let cut = OutputCut(state.next_event);
        let response = drain_through(&mut state, cut);
        boundary();
        state.recompute_budget();
        response
    }

    pub(super) fn drain_through(&self, cut: OutputCut) -> Response {
        let mut state = self.lock();
        drain_through(&mut state, cut)
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, OutputTapeState> {
        self.0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

#[cfg(target_os = "macos")]
impl DirectOutput {
    pub(super) fn push(&self, bytes: &[u8]) {
        self.output.lock().push_direct_output(self.stream, bytes);
    }

    pub(super) fn close(&self) {
        self.output.lock().close_direct_output(self.stream);
    }
}

#[cfg(target_os = "macos")]
impl DirectOutputStream {
    fn logical(self) -> LogicalStream {
        match self {
            Self::Stdout => LogicalStream::DirectStdout,
            Self::Stderr => LogicalStream::DirectStderr,
        }
    }
}

impl OutputTapeState {
    fn new(limits: OutputLimits) -> Self {
        Self {
            direct_stdout: DirectDecoder::default(),
            direct_stderr: DirectDecoder::default(),
            console_output: terminal::Stream::default(),
            console_diagnostic: terminal::Stream::default(),
            terminal_stdout: terminal::Stream::default(),
            terminal_stderr: terminal::Stream::default(),
            next_event: 0,
            events: Vec::new(),
            recovered: None,
            limits,
            budget: PendingOutputBudget::default(),
            active_truncation: None,
            drop_truncation: None,
        }
    }

    fn push_console_text(&mut self, channel: crate::worker_protocol::ConsoleChannel, text: String) {
        self.flush_direct_decoders();
        let stream = match channel {
            crate::worker_protocol::ConsoleChannel::Output => LogicalStream::ConsoleOutput,
            crate::worker_protocol::ConsoleChannel::Diagnostic => LogicalStream::ConsoleDiagnostic,
        };
        self.push_stream_text(stream, &text, None);
    }

    fn push_bounded_notice_line(&mut self, message: String) {
        self.prepare_control_output();
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
            self.omit(rendered_length, 0, 0, 1);
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
        self.flush_direct_decoders();
        self.snapshot_streams();
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
            self.omit(0, length, metadata_length, 1);
        }
        Ok(())
    }

    #[cfg(target_os = "macos")]
    fn push_direct_output(&mut self, stream: DirectOutputStream, bytes: &[u8]) {
        if bytes.is_empty() {
            return;
        }
        self.flush_other_direct_decoder(stream);
        let logical = stream.logical();
        let (complete, has_incomplete) = {
            let decoder = match stream {
                DirectOutputStream::Stdout => &mut self.direct_stdout,
                DirectOutputStream::Stderr => &mut self.direct_stderr,
            };
            decoder.bytes.extend_from_slice(bytes);
            let complete = complete_utf8_prefix(&decoder.bytes);
            let incomplete = decoder.bytes.split_off(complete);
            let complete = std::mem::replace(&mut decoder.bytes, incomplete);
            (complete, !decoder.bytes.is_empty())
        };
        let origin = has_incomplete.then(|| self.allocate_position());
        match stream {
            DirectOutputStream::Stdout => self.direct_stdout.origin = origin,
            DirectOutputStream::Stderr => self.direct_stderr.origin = origin,
        }
        if !complete.is_empty() {
            self.recompute_budget();
            self.push_stream_text(
                logical,
                &String::from_utf8_lossy(&complete),
                Some(&complete),
            );
        }
        self.recompute_budget();
    }

    #[cfg(target_os = "macos")]
    fn close_direct_output(&mut self, stream: DirectOutputStream) {
        self.flush_direct_decoder(stream.logical());
        self.finish_stream(stream.logical());
        self.allocate_position();
    }

    fn push_stream_text(&mut self, stream: LogicalStream, text: &str, source_bytes: Option<&[u8]>) {
        self.snapshot_other_streams(stream);
        let mut terminal = self.take_terminal_stream(stream);
        let update = terminal.ingest(text);
        let source = source_bytes
            .filter(|_| update.text == text)
            .and_then(|bytes| {
                let active_suffix = match update.active_suffix {
                    Some(offset) => Some(lossy_source_prefix_length(bytes, offset)?),
                    None => None,
                };
                Some(SourceText {
                    bytes: bytes.len(),
                    active_suffix,
                })
            });
        let before = self.next_event;
        self.apply_stream_update(&mut terminal, update, source, None);
        let observation = if self.next_event == before {
            self.allocate_position()
        } else {
            self.next_event - 1
        };
        terminal.observe(observation);
        self.put_terminal_stream(stream, terminal);
    }

    fn finish_stream(&mut self, stream: LogicalStream) {
        let mut terminal = self.take_terminal_stream(stream);
        if let Some(observation) = terminal.last_observation() {
            let update = terminal.finish();
            self.apply_stream_update(&mut terminal, update, None, Some(observation));
        }
        self.put_terminal_stream(stream, terminal);
    }

    fn finish_streams(&mut self) {
        let mut streams = [
            LogicalStream::ConsoleOutput,
            LogicalStream::ConsoleDiagnostic,
            LogicalStream::DirectStdout,
            LogicalStream::DirectStderr,
        ];
        streams.sort_by_key(|stream| self.terminal_stream(*stream).last_observation());
        for stream in streams {
            self.finish_stream(stream);
        }
    }

    fn snapshot_stream(&mut self, stream: LogicalStream) {
        let mut terminal = self.take_terminal_stream(stream);
        if let Some(observation) = terminal.last_observation() {
            let update = terminal.snapshot();
            self.apply_stream_update(&mut terminal, update, None, Some(observation));
        }
        self.put_terminal_stream(stream, terminal);
    }

    fn snapshot_streams(&mut self) {
        let mut streams = [
            LogicalStream::ConsoleOutput,
            LogicalStream::ConsoleDiagnostic,
            LogicalStream::DirectStdout,
            LogicalStream::DirectStderr,
        ];
        streams.sort_by_key(|stream| self.terminal_stream(*stream).last_observation());
        for stream in streams {
            self.snapshot_stream(stream);
        }
    }

    fn snapshot_other_streams(&mut self, current: LogicalStream) {
        // Keep same-stream fragments cheap, but publish an earlier stream before
        // later-stream output can consume its event or byte budget.
        let mut streams = [
            LogicalStream::ConsoleOutput,
            LogicalStream::ConsoleDiagnostic,
            LogicalStream::DirectStdout,
            LogicalStream::DirectStderr,
        ];
        streams.sort_by_key(|stream| self.terminal_stream(*stream).last_observation());
        for stream in streams {
            if stream != current {
                self.snapshot_stream(stream);
            }
        }
    }

    fn apply_stream_update(
        &mut self,
        terminal: &mut terminal::Stream,
        update: terminal::Update,
        source: Option<SourceText>,
        observation: Option<u64>,
    ) {
        match update.prior {
            terminal::PriorLine::Continue => {}
            terminal::PriorLine::Finalized => self.detach_active_events(terminal),
            terminal::PriorLine::Replace => self.remove_active_events(terminal),
        }
        if update.text.is_empty() {
            return;
        }

        let original_length = update.text.len();
        let original_bytes = source.map_or(original_length, |source| source.bytes);
        let active_source_offset = update.active_suffix.map(|text_offset| {
            source
                .and_then(|source| source.active_suffix)
                .unwrap_or(text_offset)
                .min(original_bytes)
        });
        if self.budget.dropping_ordinary_output {
            self.omit_stream_text(terminal, original_bytes, active_source_offset, 0);
            return;
        }
        let remaining = self
            .limits
            .text_bytes
            .saturating_sub(self.budget.text_bytes);
        let retained = if self.budget.events >= self.limits.events {
            0
        } else if original_bytes <= remaining {
            original_length
        } else if original_bytes == original_length {
            utf8_prefix_length(&update.text, remaining)
        } else {
            0
        };
        let retained_bytes = if retained == original_length {
            original_bytes
        } else {
            retained
        };
        let active_suffix = update
            .active_suffix
            .filter(|text_offset| *text_offset < retained)
            .map(|text_offset| ActiveSuffix {
                text_offset,
                source_offset: active_source_offset
                    .unwrap_or(text_offset)
                    .min(retained_bytes),
            });
        if retained > 0 {
            let retained_text = if retained == original_length {
                update.text.into_boxed_str()
            } else {
                Box::<str>::from(&update.text[..retained])
            };
            let event = OutputEvent::Text {
                text: retained_text,
                text_bytes: retained_bytes,
                active_suffix,
            };
            let sequence = match observation {
                Some(observation) => {
                    self.retain_ordinary_at(observation, event, retained_bytes, 0, 0)
                }
                None => self.retain_ordinary(event, retained_bytes, 0, 0),
            };
            if active_suffix.is_some() {
                terminal.push_active_event(sequence);
            }
        }
        if retained_bytes < original_bytes {
            self.omit_stream_text(
                terminal,
                original_bytes,
                active_source_offset,
                retained_bytes,
            );
        }
    }

    fn omit_stream_text(
        &mut self,
        terminal: &mut terminal::Stream,
        original_bytes: usize,
        active_source_offset: Option<usize>,
        retained_bytes: usize,
    ) {
        let stable_bytes = active_source_offset.unwrap_or(original_bytes);
        let retained_stable = retained_bytes.min(stable_bytes);
        let retained_active = retained_bytes.saturating_sub(retained_stable);
        let omitted_stable = stable_bytes.saturating_sub(retained_stable);
        let omitted_active = original_bytes
            .saturating_sub(stable_bytes)
            .saturating_sub(retained_active);

        if omitted_stable > 0 {
            self.omit(omitted_stable, 0, 0, 1);
        }
        if omitted_active > 0 {
            let omitted_events = usize::from(omitted_stable == 0);
            let sequence = self.omit(omitted_active, 0, 0, omitted_events);
            terminal.push_active_omission(sequence, omitted_active, omitted_events);
        }
    }

    fn remove_active_events(&mut self, terminal: &mut terminal::Stream) {
        let mut released = false;
        for sequence in terminal.take_active_events() {
            let Ok(index) = self
                .events
                .binary_search_by_key(&sequence, |(sequence, _)| *sequence)
            else {
                continue;
            };
            let OutputEvent::Text {
                text,
                text_bytes,
                active_suffix: Some(active_suffix),
                ..
            } = &mut self.events[index].1
            else {
                continue;
            };
            let retained_bytes = (*text_bytes).min(active_suffix.source_offset);
            let removed = *text_bytes - retained_bytes;
            released |= removed > 0;
            self.budget.text_bytes = self.budget.text_bytes.saturating_sub(removed);
            if active_suffix.text_offset == 0 {
                self.events.remove(index);
                self.budget.events = self.budget.events.saturating_sub(1);
            } else {
                *text = Box::from(&text[..active_suffix.text_offset]);
                *text_bytes = retained_bytes;
                self.events[index].1.clear_active_suffix();
            }
        }
        for omission in terminal.take_active_omissions() {
            let Ok(index) = self
                .events
                .binary_search_by_key(&omission.sequence, |(sequence, _)| *sequence)
            else {
                continue;
            };
            let remove = {
                let OutputEvent::Truncated(truncation) = &mut self.events[index].1 else {
                    continue;
                };
                truncation.text_bytes = truncation.text_bytes.saturating_sub(omission.text_bytes);
                truncation.events = truncation.events.saturating_sub(omission.events);
                truncation.is_empty()
            };
            released = true;
            if remove {
                self.events.remove(index);
                if self.active_truncation == Some(omission.sequence) {
                    self.active_truncation = None;
                }
            }
        }
        if released {
            self.drop_truncation = None;
            self.budget.dropping_ordinary_output = false;
        }
    }

    fn detach_active_events(&mut self, terminal: &mut terminal::Stream) {
        for sequence in terminal.take_active_events() {
            if let Ok(index) = self
                .events
                .binary_search_by_key(&sequence, |(sequence, _)| *sequence)
            {
                self.events[index].1.clear_active_suffix();
            }
        }
        terminal.take_active_omissions();
    }

    fn seal_streams(&mut self) {
        for stream in [
            LogicalStream::ConsoleOutput,
            LogicalStream::ConsoleDiagnostic,
            LogicalStream::DirectStdout,
            LogicalStream::DirectStderr,
        ] {
            let mut terminal = self.take_terminal_stream(stream);
            self.detach_active_events(&mut terminal);
            self.put_terminal_stream(stream, terminal);
        }
    }

    fn prepare_control_output(&mut self) {
        self.flush_direct_decoders();
        self.finish_streams();
    }

    fn terminal_stream(&self, stream: LogicalStream) -> &terminal::Stream {
        match stream {
            LogicalStream::ConsoleOutput => &self.console_output,
            LogicalStream::ConsoleDiagnostic => &self.console_diagnostic,
            LogicalStream::DirectStdout => &self.terminal_stdout,
            LogicalStream::DirectStderr => &self.terminal_stderr,
        }
    }

    fn take_terminal_stream(&mut self, stream: LogicalStream) -> terminal::Stream {
        match stream {
            LogicalStream::ConsoleOutput => std::mem::take(&mut self.console_output),
            LogicalStream::ConsoleDiagnostic => std::mem::take(&mut self.console_diagnostic),
            LogicalStream::DirectStdout => std::mem::take(&mut self.terminal_stdout),
            LogicalStream::DirectStderr => std::mem::take(&mut self.terminal_stderr),
        }
    }

    fn put_terminal_stream(&mut self, stream: LogicalStream, terminal: terminal::Stream) {
        match stream {
            LogicalStream::ConsoleOutput => self.console_output = terminal,
            LogicalStream::ConsoleDiagnostic => self.console_diagnostic = terminal,
            LogicalStream::DirectStdout => self.terminal_stdout = terminal,
            LogicalStream::DirectStderr => self.terminal_stderr = terminal,
        }
    }

    fn flush_direct_decoder(&mut self, stream: LogicalStream) {
        let decoder = match stream {
            LogicalStream::DirectStdout => &mut self.direct_stdout,
            LogicalStream::DirectStderr => &mut self.direct_stderr,
            LogicalStream::ConsoleOutput | LogicalStream::ConsoleDiagnostic => return,
        };
        let bytes = std::mem::take(&mut decoder.bytes);
        decoder.origin = None;
        if !bytes.is_empty() {
            self.recompute_budget();
            self.push_stream_text(stream, &String::from_utf8_lossy(&bytes), Some(&bytes));
        }
    }

    fn flush_direct_decoders(&mut self) {
        let stdout = self.direct_stdout.origin;
        let stderr = self.direct_stderr.origin;
        if stdout <= stderr {
            self.flush_direct_decoder(LogicalStream::DirectStdout);
            self.flush_direct_decoder(LogicalStream::DirectStderr);
        } else {
            self.flush_direct_decoder(LogicalStream::DirectStderr);
            self.flush_direct_decoder(LogicalStream::DirectStdout);
        }
    }

    #[cfg(target_os = "macos")]
    fn flush_other_direct_decoder(&mut self, stream: DirectOutputStream) {
        match stream {
            DirectOutputStream::Stdout => self.flush_direct_decoder(LogicalStream::DirectStderr),
            DirectOutputStream::Stderr => self.flush_direct_decoder(LogicalStream::DirectStdout),
        }
    }

    fn retain_ordinary(
        &mut self,
        event: OutputEvent,
        text_bytes: usize,
        image_bytes: usize,
        image_metadata_bytes: usize,
    ) -> u64 {
        self.budget.text_bytes = self.budget.text_bytes.saturating_add(text_bytes);
        self.budget.image_bytes = self.budget.image_bytes.saturating_add(image_bytes);
        self.budget.image_metadata_bytes = self
            .budget
            .image_metadata_bytes
            .saturating_add(image_metadata_bytes);
        self.budget.events = self.budget.events.saturating_add(1);
        self.push_event(event)
    }

    fn retain_ordinary_at(
        &mut self,
        sequence: u64,
        event: OutputEvent,
        text_bytes: usize,
        image_bytes: usize,
        image_metadata_bytes: usize,
    ) -> u64 {
        self.budget.text_bytes = self.budget.text_bytes.saturating_add(text_bytes);
        self.budget.image_bytes = self.budget.image_bytes.saturating_add(image_bytes);
        self.budget.image_metadata_bytes = self
            .budget
            .image_metadata_bytes
            .saturating_add(image_metadata_bytes);
        self.budget.events = self.budget.events.saturating_add(1);
        // Deferred volatile projections reuse their ingestion position so a cut
        // cannot move them behind output observed from another stream.
        let index = self
            .events
            .binary_search_by_key(&sequence, |(candidate, _)| *candidate)
            .expect_err("an observation position can retain only one event");
        self.events.insert(index, (sequence, event));
        sequence
    }

    fn push_control(&mut self, event: OutputEvent) {
        self.prepare_control_output();
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
    ) -> u64 {
        self.budget.dropping_ordinary_output = true;
        let sequence = if let Some(sequence) = self.active_truncation {
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
            // Omitted publications still occupy observation-order positions, so a
            // later cut can seal counts without retaining one event per chunk.
            self.allocate_position();
            sequence
        } else {
            let sequence = self.push_event(OutputEvent::Truncated(Truncation {
                text_bytes,
                image_bytes,
                image_metadata_bytes,
                events,
            }));
            self.active_truncation = Some(sequence);
            sequence
        };
        self.drop_truncation = Some(sequence);
        sequence
    }

    fn allocate_position(&mut self) -> u64 {
        let position = self.next_event;
        self.next_event = self
            .next_event
            .checked_add(1)
            .expect("output tape position overflowed");
        position
    }

    fn seal_truncation(&mut self) {
        self.active_truncation = None;
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
                OutputEvent::Text { text_bytes, .. } => {
                    budget.text_bytes = budget.text_bytes.saturating_add(*text_bytes);
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
                OutputEvent::Truncated(_) => {}
                OutputEvent::ServerNotice(_) | OutputEvent::ServerFailure(_) => {}
            }
        }
        if self.active_truncation.is_some_and(|sequence| {
            self.events
                .binary_search_by_key(&sequence, |(sequence, _)| *sequence)
                .is_err()
        }) {
            self.active_truncation = None;
        }
        if self.drop_truncation.is_some_and(|sequence| {
            self.events
                .binary_search_by_key(&sequence, |(sequence, _)| *sequence)
                .is_err()
        }) {
            self.drop_truncation = None;
        }
        budget.dropping_ordinary_output = self.drop_truncation.is_some();
        self.budget = budget;
    }
}

fn drain_through(state: &mut OutputTapeState, cut: OutputCut) -> Response {
    let boundary = state
        .events
        .partition_point(|(sequence, _)| *sequence < cut.0);
    let remaining = state.events.split_off(boundary);
    let events = std::mem::replace(&mut state.events, remaining);
    let mut output = ResponseBuilder::from_response(state.recovered.take().unwrap_or_default());

    for (_, event) in events {
        match event {
            OutputEvent::Text { text, .. } => output.text(text.into_string()),
            OutputEvent::WorkerImage {
                data,
                mime_type,
                artifact,
            } => output.image(data.into_string(), mime_type.into_string(), artifact),
            OutputEvent::ServerNotice(message) => output.notice_line(message),
            OutputEvent::BoundedServerNotice(message) => output.notice_line(message.into_string()),
            OutputEvent::ServerFailure(failure) => output.send_failure(failure),
            OutputEvent::Truncated(truncated) => output.truncation(truncated),
        }
    }

    state.recompute_budget();
    output.finish()
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

fn lossy_source_prefix_length(bytes: &[u8], projected_prefix: usize) -> Option<usize> {
    let mut source = 0;
    let mut projected = 0;
    loop {
        match std::str::from_utf8(&bytes[source..]) {
            Ok(valid) => {
                let suffix = projected_prefix.checked_sub(projected)?;
                return (suffix <= valid.len()).then_some(source + suffix);
            }
            Err(error) => {
                let valid = error.valid_up_to();
                if projected_prefix <= projected.saturating_add(valid) {
                    return Some(source + projected_prefix - projected);
                }
                source += valid;
                projected += valid;

                let invalid = error.error_len().unwrap_or(bytes.len() - source);
                source += invalid;
                projected += '\u{fffd}'.len_utf8();
                if projected_prefix == projected {
                    return Some(source);
                }
                if projected_prefix < projected {
                    return None;
                }
            }
        }
    }
}

fn utf8_prefix_length(text: &str, limit: usize) -> usize {
    let mut length = text.len().min(limit);
    while !text.is_char_boundary(length) {
        length -= 1;
    }
    length
}

#[cfg(test)]
mod tests {
    use super::*;

    use crate::worker_protocol::ConsoleChannel::{Diagnostic, Output};

    fn limits(text_bytes: usize, image_bytes: usize, events: usize) -> OutputLimits {
        OutputLimits {
            text_bytes,
            image_bytes,
            image_metadata_bytes: 1_024,
            events,
        }
    }

    fn limits_with_image_metadata(
        text_bytes: usize,
        image_bytes: usize,
        image_metadata_bytes: usize,
        events: usize,
    ) -> OutputLimits {
        OutputLimits {
            text_bytes,
            image_bytes,
            image_metadata_bytes,
            events,
        }
    }

    fn response_text(mut response: Response) -> String {
        std::mem::take(&mut response.content)
            .into_iter()
            .filter_map(|content| match content {
                Content::Text(text) => Some(text),
                Content::Image { .. } => None,
            })
            .collect()
    }

    fn response_content(mut response: Response) -> Vec<Content> {
        std::mem::take(&mut response.content)
    }

    fn assert_text(response: Response, expected: &str) {
        assert_eq!(response_text(response), expected);
    }

    fn assert_single_image(response: Response, data: &str, mime_type: &str) {
        assert!(matches!(
            response_content(response).as_slice(),
            [Content::Image {
                data: actual_data,
                mime_type: actual_mime_type,
                ..
            }] if actual_data == data && actual_mime_type == mime_type
        ));
    }

    fn text_response(text: &str) -> Response {
        let mut builder = ResponseBuilder::new();
        builder.text(text);
        builder.finish()
    }

    #[test]
    fn text_budget_retains_below_exact_and_prefix_above_limit() {
        for (size, expected, omitted) in [(4, 4, None), (5, 5, None), (6, 5, Some(1))] {
            let output = OutputTape::with_limits(limits(5, 20, 20));
            output.push_console_text(Output, "x".repeat(size));

            let text = response_text(output.take());
            assert!(text.starts_with(&"x".repeat(expected)));
            match omitted {
                Some(omitted) => assert!(text.ends_with(&format!(
                    "[output truncated: omitted {omitted} text bytes and 0 encoded image bytes across 1 event]"
                ))),
                None => assert_eq!(text, "x".repeat(expected)),
            }
        }
    }

    #[test]
    fn one_chunk_larger_than_the_entire_budget_is_copied_only_to_the_limit() {
        let output = OutputTape::with_limits(limits(5, 20, 20));
        output.push_console_text(Output, "x".repeat(100));

        {
            let state = output.lock();
            let OutputEvent::Text { text, .. } = &state.events[0].1 else {
                panic!("the retained prefix must be stored as console text")
            };
            assert_eq!(text.len(), 5);
        }

        assert_text(
            output.take(),
            "xxxxx\n[output truncated: omitted 95 text bytes and 0 encoded image bytes across 1 event]",
        );
        output.push_console_text(Output, "fresh");
        assert_text(output.take(), "fresh");
    }

    #[test]
    fn event_budget_retains_below_exact_and_aggregates_later_events() {
        let output = OutputTape::with_limits(limits(100, 100, 2));
        output.push_console_text(Output, "a");
        output.push_console_text(Diagnostic, "b");
        output.push_console_text(Output, "c");
        output.push_console_text(Diagnostic, "de");

        assert_text(
            output.take(),
            "ab\n[output truncated: omitted 3 text bytes and 0 encoded image bytes across 2 events]",
        );
        output.push_console_text(Output, "fresh");
        assert_text(output.take(), "fresh");
    }

    #[test]
    fn image_budget_is_all_or_nothing_and_preserves_order() {
        for data in ["123", "1234"] {
            let accepted = OutputTape::with_limits(limits(100, 4, 20));
            accepted.push_console_text(Output, "before");
            accepted.push_image(data.to_string(), "image/test".to_string(), None);
            accepted.push_console_text(Diagnostic, "after");
            let content = response_content(accepted.take());
            assert!(matches!(&content[0], Content::Text(text) if text == "before"));
            assert!(matches!(
                &content[1],
                Content::Image { data: actual, mime_type, .. }
                    if actual == data && mime_type == "image/test"
            ));
            assert!(matches!(&content[2], Content::Text(text) if text == "after"));
        }

        let rejected = OutputTape::with_limits(limits(100, 4, 20));
        rejected.push_console_text(Output, "before");
        rejected.push_image("12345".to_string(), "image/test".to_string(), None);
        rejected.push_console_text(Output, "discarded");
        assert_text(
            rejected.take(),
            "before\n[output truncated: omitted 9 text bytes, 5 encoded image bytes, and 10 image metadata bytes across 2 events]",
        );
        rejected.push_image("1234".to_string(), "image/test".to_string(), None);
        assert_single_image(rejected.take(), "1234", "image/test");
    }

    #[test]
    fn image_metadata_budget_retains_below_exact_and_omits_above_limit() {
        for mime_type in ["abc", "abcd"] {
            let output = OutputTape::with_limits(limits_with_image_metadata(100, 100, 4, 20));
            output.push_image("1".to_string(), mime_type.to_string(), None);
            assert!(matches!(
                response_content(output.take()).as_slice(),
                [Content::Image { mime_type: retained, .. }] if retained == mime_type
            ));
        }

        let output = OutputTape::with_limits(limits_with_image_metadata(100, 100, 4, 20));
        output.push_image("1".to_string(), "abcde".to_string(), None);
        assert_text(
            output.take(),
            "[output truncated: omitted 0 text bytes, 1 encoded image bytes, and 5 image metadata bytes across 1 event]",
        );
        output.push_image("1".to_string(), "abcd".to_string(), None);
        assert_single_image(output.take(), "1", "abcd");
    }

    #[test]
    fn overflow_skips_image_artifact_creation() {
        let output = OutputTape::with_limits(limits(100, 2, 20));
        let called = std::cell::Cell::new(false);
        output
            .push_image_with_artifact("123".to_string(), "image/test".to_string(), |_, _| {
                called.set(true);
                Ok(None)
            })
            .unwrap();

        assert!(!called.get());
        assert_text(
            output.take(),
            "[output truncated: omitted 0 text bytes, 3 encoded image bytes, and 10 image metadata bytes across 1 event]",
        );
    }

    #[test]
    fn control_failures_and_process_outcomes_survive_truncation() {
        let output = OutputTape::with_limits(limits(3, 3, 1));
        output.push_console_text(Output, "overflow");
        output.push_notice_line("input requested: \"prompt> \"");
        output.push_failure(
            SendFailure::from("worker transport failed".to_string())
                .worker_outcome(Some(super::super::WorkerProcessOutcome::Exited(86)))
                .worker_stopped(),
        );

        let response = output.take();
        assert!(response.is_error);
        assert_text(
            response,
            "ove\n[output truncated: omitted 5 text bytes and 0 encoded image bytes across 1 event]\n[input requested: \"prompt> \"]\n[worker transport failed]\n[worker exited with status 86]\n[worker stopped: in-memory state lost]",
        );
    }

    #[test]
    fn a_cut_seals_truncation_counts_and_leaves_later_output_pending() {
        let output = OutputTape::with_limits(limits(3, 10, 10));
        output.push_console_text(Output, "four");
        let cut = output.cut();
        output.push_console_text(Output, "zz");

        assert_text(
            output.drain_through(cut),
            "fou\n[output truncated: omitted 1 text bytes and 0 encoded image bytes across 1 event]",
        );
        assert_text(
            output.take(),
            "[output truncated: omitted 2 text bytes and 0 encoded image bytes across 1 event]",
        );

        output.push_console_text(Output, "new");
        assert_text(output.take(), "new");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn direct_utf8_truncates_at_scalar_boundaries_and_keeps_stream_order() {
        let output = OutputTape::with_limits(limits(2, 100, 100));
        let stdout = output.direct_stdout();
        stdout.push(&[0xe2, 0x82, 0xac]);
        assert_text(
            output.take(),
            "[output truncated: omitted 3 text bytes and 0 encoded image bytes across 1 event]",
        );
        stdout.push(&[0xac, 0xff]);
        assert_text(output.take(), "��");

        let output = OutputTape::with_limits(limits(2, 100, 100));
        output.direct_stderr().push(&[0xe2]);
        output.direct_stdout().push(&[0xe2]);
        output.push_console_text(Output, "overflow");
        assert_text(
            output.take(),
            "��\n[output truncated: omitted 8 text bytes and 0 encoded image bytes across 1 event]",
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn direct_utf8_respects_cut_and_prelude_boundaries() {
        let output = OutputTape::with_limits(limits(100, 100, 100));
        let stdout = output.direct_stdout();
        stdout.push(&[0xe2, 0x82]);
        let cut = output.cut();
        assert_text(output.drain_through(cut), "");
        stdout.push(&[0xac]);
        assert_text(output.take(), "€");

        let output = OutputTape::with_limits(limits(100, 100, 100));
        let stdout = output.direct_stdout();
        stdout.push(&[0xe2, 0x82]);
        output.push_console_text(Output, "idle text");
        let mut prelude = output.take_prelude();
        stdout.push(&[0xac]);
        prelude.extend_cell_after_idle_prelude(output.take());
        assert_text(prelude, "�idle text\n[output produced while idle]\n�");

        let output = OutputTape::with_limits(limits(100, 100, 100));
        output.direct_stdout().push(&[0xe2]);
        output.push_image("image".to_string(), "image/test".to_string(), None);
        let content = response_content(output.take_prelude());
        assert!(matches!(&content[0], Content::Text(text) if text == "�"));
        assert!(matches!(&content[1], Content::Image { data, .. } if data == "image"));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn mixed_text_channels_direct_streams_images_and_notices_keep_order() {
        let output = OutputTape::with_limits(limits(100, 100, 100));
        output.push_console_text(Output, "console output|");
        output.push_console_text(Diagnostic, "console diagnostic|");
        output.direct_stdout().push(b"stdout|");
        output.direct_stderr().push(b"stderr|");
        output.push_image("image".to_string(), "image/test".to_string(), None);
        output.push_notice_line("server notice");

        let content = response_content(output.take());
        assert!(matches!(
            &content[0],
            Content::Text(text)
                if text == "console output|console diagnostic|stdout|stderr|"
        ));
        assert!(matches!(&content[1], Content::Image { data, .. } if data == "image"));
        assert!(matches!(&content[2], Content::Text(text) if text == "\n[server notice]\n"));
    }

    #[test]
    fn canonical_builder_delimits_idle_preludes_and_logical_regions() {
        let mut prelude = text_response("idle output");
        prelude.extend_cell_after_idle_prelude(text_response("cell output"));
        assert_text(
            prelude,
            "idle output\n[output produced while idle]\ncell output",
        );

        let mut prelude_only = text_response("idle only");
        prelude_only.extend_cell_after_idle_prelude(Response::default());
        assert_text(prelude_only, "idle only");

        let mut cell_only = Response::default();
        cell_only.extend_cell_after_idle_prelude(text_response("cell only"));
        assert_text(cell_only, "cell only");

        let mut logical = text_response("first");
        logical.extend_logical_region(text_response("second"));
        assert_text(logical, "first\nsecond");
    }

    #[test]
    fn every_terminal_state_uses_the_canonical_projection() {
        let cases = [
            (SendResponse::Completed(Response::default()), "[done]"),
            (
                SendResponse::Running(Response::default()),
                "\n[running; poll with an empty send]",
            ),
            (
                SendResponse::InputRequested(Response::default()),
                "\n[waiting for stdin]",
            ),
            (SendResponse::Idle(Response::default()), "\n[idle]"),
            (
                SendResponse::ReplacementStarting(Response::default()),
                "[worker starting]",
            ),
            (
                SendResponse::ReplacementReady(Response::default()),
                "[idle]",
            ),
        ];
        for (response, expected) in cases {
            assert_text(render_response(response), expected);
        }

        let mut failed = ResponseBuilder::new();
        failed.server_failure("infrastructure failed");
        assert_text(
            render_response(SendResponse::Idle(failed.finish())),
            "[infrastructure failed]",
        );
    }

    #[test]
    fn image_only_completion_does_not_add_done() {
        let mut builder = ResponseBuilder::new();
        builder.image("image".to_string(), "image/test".to_string(), None);
        let content = response_content(render_response(SendResponse::Completed(builder.finish())));
        assert!(matches!(content.as_slice(), [Content::Image { data, .. }] if data == "image"));
    }
}
