use std::sync::mpsc::SyncSender;
use std::sync::{Arc, Mutex};

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

#[derive(Default)]
struct OutputTapeState {
    direct_stdout: Vec<u8>,
    direct_stderr: Vec<u8>,
    next_event: u64,
    events: Vec<(u64, OutputEvent)>,
}

#[derive(Clone, Copy)]
pub(super) struct OutputCheckpoint(u64);

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
        text: String,
    },
    /// An image from a worker `image` sideband frame, already persisted when enabled.
    WorkerImage {
        data: String,
        mime_type: String,
        artifact: Option<crate::transcript::Artifact>,
    },
    /// An unbracketed server-owned lifecycle, state, or input notice.
    ServerNotice {
        message: String,
        /// End the notice with a newline before later worker output arrives.
        terminate_line: bool,
    },
    /// A server infrastructure, transport, or protocol failure.
    ///
    /// Language errors are normal evaluation output and do not use this event.
    ServerFailure(SendFailure),
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
    acknowledgment: Option<SyncSender<ResponseAcknowledgment>>,
}

pub(super) enum ResponseAcknowledgment {
    Delivered,
    Unclaimed(Response),
}

/// Releases an explicit restart after the interrupted `send` reply is written.
pub(crate) struct ResponseDelivery(Option<SyncSender<ResponseAcknowledgment>>);

pub(crate) enum Content {
    Text(String),
    Image {
        data: String,
        mime_type: String,
        artifact: Option<crate::transcript::Artifact>,
    },
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
        let content = std::mem::take(&mut self.content);
        let is_error = self.is_error;
        let delivery = self
            .acknowledgment
            .take()
            .map(|acknowledgment| ResponseDelivery(Some(acknowledgment)));
        (content, is_error, delivery)
    }

    pub(super) fn extend(&mut self, mut other: Self) {
        if other.acknowledgment.is_some() {
            assert!(
                self.acknowledgment.is_none(),
                "a response can carry only one acknowledgment"
            );
            self.acknowledgment = other.acknowledgment.take();
        }
        for content in std::mem::take(&mut other.content) {
            match content {
                Content::Text(text) => self.push_text(text),
                Content::Image {
                    data,
                    mime_type,
                    artifact,
                } => self.push_image(data, mime_type, artifact),
            }
        }
        self.is_error |= other.is_error;
    }

    pub(super) fn extend_at_boundary(&mut self, other: Self) {
        if matches!(self.content.last(), Some(Content::Text(text)) if !text.ends_with('\n'))
            && matches!(other.content.first(), Some(Content::Text(_)))
        {
            self.push_text("\n");
        }
        self.extend(other);
    }

    pub(super) fn acknowledge_with(&mut self, acknowledgment: SyncSender<ResponseAcknowledgment>) {
        assert!(
            self.acknowledgment.is_none(),
            "a response can carry only one acknowledgment"
        );
        self.acknowledgment = Some(acknowledgment);
    }

    fn push_text(&mut self, text: impl Into<String>) {
        let text = text.into();
        if text.is_empty() {
            return;
        }
        if let Some(Content::Text(output)) = self.content.last_mut() {
            output.push_str(&text);
        } else {
            self.content.push(Content::Text(text));
        }
    }

    fn push_image(
        &mut self,
        data: String,
        mime_type: String,
        artifact: Option<crate::transcript::Artifact>,
    ) {
        self.content.push(Content::Image {
            data,
            mime_type,
            artifact,
        });
    }

    fn is_empty(&self) -> bool {
        self.content.is_empty()
    }

    fn is_error(&self) -> bool {
        self.is_error
    }

    fn text_needs_newline(&self) -> bool {
        !matches!(self.content.last(), Some(Content::Text(text)) if text.ends_with('\n'))
    }

    fn push_line(&mut self, text: impl Into<String>) {
        if !self.is_empty() && self.text_needs_newline() {
            self.push_text("\n");
        }
        self.push_text(text);
    }

    pub(super) fn push_notice(&mut self, message: impl Into<String>) {
        self.push_line(render_notice(message));
    }

    /// Adds a server notice and ends its line for any output appended later.
    pub(super) fn push_notice_line(&mut self, message: impl Into<String>) {
        self.push_notice(message);
        self.push_text("\n");
    }

    pub(super) fn push_server_failure(&mut self, message: impl Into<String>) {
        self.push_notice(message);
        self.mark_error();
    }

    pub(super) fn push_tool_error(&mut self, message: impl Into<String>) {
        self.push_line(message);
        self.mark_error();
    }

    pub(super) fn mark_error(&mut self) {
        self.is_error = true;
    }
}

impl ResponseDelivery {
    pub(crate) fn complete(mut self) {
        if let Some(acknowledgment) = self.0.take() {
            let _ = acknowledgment.send(ResponseAcknowledgment::Delivered);
        }
    }
}

impl Drop for Response {
    fn drop(&mut self) {
        if let Some(acknowledgment) = self.acknowledgment.take() {
            let response = Self {
                content: std::mem::take(&mut self.content),
                is_error: self.is_error,
                acknowledgment: None,
            };
            let _ = acknowledgment.send(ResponseAcknowledgment::Unclaimed(response));
        }
    }
}

impl OutputTape {
    pub(super) fn new() -> Self {
        Self(Arc::new(Mutex::new(OutputTapeState::default())))
    }

    #[cfg(target_os = "macos")]
    pub(super) fn direct_stdout(&self) -> DirectOutput {
        DirectOutput {
            output: self.clone(),
            stream: DirectOutputStream::Stdout,
        }
    }

    #[cfg(target_os = "macos")]
    pub(super) fn direct_stderr(&self) -> DirectOutput {
        DirectOutput {
            output: self.clone(),
            stream: DirectOutputStream::Stderr,
        }
    }

    pub(super) fn push_console_text(
        &self,
        channel: crate::worker_protocol::ConsoleChannel,
        text: impl Into<String>,
    ) {
        let text = text.into();
        if !text.is_empty() {
            self.push_event(OutputEvent::WorkerConsoleText { channel, text });
        }
    }

    pub(super) fn push_image(
        &self,
        data: String,
        mime_type: String,
        artifact: Option<crate::transcript::Artifact>,
    ) {
        self.push_event(OutputEvent::WorkerImage {
            data,
            mime_type,
            artifact,
        });
    }

    /// Publishes a server notice that ends its line before later worker output.
    pub(super) fn push_notice_line(&self, message: impl Into<String>) {
        self.push_event(OutputEvent::ServerNotice {
            message: message.into(),
            terminate_line: true,
        });
    }

    pub(super) fn push_failure(&self, failure: SendFailure) {
        self.push_event(OutputEvent::ServerFailure(failure));
    }

    pub(super) fn checkpoint(&self) -> OutputCheckpoint {
        OutputCheckpoint(self.lock().next_event)
    }

    pub(super) fn take(&self) -> Response {
        self.take_until(OutputCheckpoint(u64::MAX))
    }

    pub(super) fn take_until(&self, checkpoint: OutputCheckpoint) -> Response {
        let mut state = self.lock();
        let boundary = state
            .events
            .partition_point(|(sequence, _)| *sequence < checkpoint.0);
        let remaining = state.events.split_off(boundary);
        let events = std::mem::replace(&mut state.events, remaining);
        let mut output = Response::default();

        for (_, event) in events {
            match event {
                OutputEvent::DirectStdout(event) => {
                    append_direct_output(&mut output, &mut state.direct_stdout, event);
                }
                OutputEvent::DirectStderr(event) => {
                    append_direct_output(&mut output, &mut state.direct_stderr, event);
                }
                OutputEvent::WorkerConsoleText { channel, text } => match channel {
                    crate::worker_protocol::ConsoleChannel::Output
                    | crate::worker_protocol::ConsoleChannel::Diagnostic => output.push_text(text),
                },
                OutputEvent::WorkerImage {
                    data,
                    mime_type,
                    artifact,
                } => output.push_image(data, mime_type, artifact),
                OutputEvent::ServerNotice {
                    message,
                    terminate_line,
                } => {
                    if terminate_line {
                        output.push_notice_line(message);
                    } else {
                        output.push_notice(message);
                    }
                }
                OutputEvent::ServerFailure(SendFailure {
                    message,
                    worker_stopped,
                    worker_outcome,
                    ..
                }) => {
                    output.push_server_failure(message);
                    if let Some(outcome) = worker_outcome {
                        output.push_notice(outcome.diagnostic());
                    }
                    if worker_stopped {
                        output.push_notice(WORKER_STOPPED_NOTICE);
                    }
                }
            }
        }

        output
    }

    fn push_event(&self, event: OutputEvent) {
        let mut state = self.lock();
        let sequence = state.next_event;
        state.next_event = state.next_event.wrapping_add(1);
        state.events.push((sequence, event));
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
        self.push_event(DirectOutputEvent::Bytes(bytes.to_vec()));
    }

    pub(super) fn close(&self) {
        self.push_event(DirectOutputEvent::Closed);
    }

    fn push_event(&self, event: DirectOutputEvent) {
        let event = match self.stream {
            DirectOutputStream::Stdout => OutputEvent::DirectStdout(event),
            DirectOutputStream::Stderr => OutputEvent::DirectStderr(event),
        };
        self.output.push_event(event);
    }
}

fn append_direct_output(output: &mut Response, pending: &mut Vec<u8>, event: DirectOutputEvent) {
    match event {
        DirectOutputEvent::Bytes(bytes) => {
            pending.extend_from_slice(&bytes);
            let complete = complete_utf8_prefix(pending);
            let incomplete = pending.split_off(complete);
            let complete = std::mem::replace(pending, incomplete);
            output.push_text(String::from_utf8_lossy(&complete));
        }
        DirectOutputEvent::Closed => {
            output.push_text(String::from_utf8_lossy(pending));
            pending.clear();
        }
    }
}

pub(super) fn project_completed(mut output: Response) -> Response {
    if output.is_empty() {
        output.push_notice("done");
    }
    output
}

pub(super) fn project_replacement_ready(mut output: Response) -> Response {
    output.push_notice(WORKER_IDLE_NOTICE);
    output
}

pub(super) fn project_idle(mut output: Response) -> Response {
    if !output.is_error() {
        append_state_banner(&mut output, WORKER_IDLE_NOTICE);
    }
    output
}

pub(super) fn render_response(response: SendResponse) -> Response {
    match response {
        SendResponse::Completed(output) => project_completed(output),
        SendResponse::Failed(output) => output,
        SendResponse::InputRequested(mut output) => {
            append_input_banner(&mut output);
            output
        }
        SendResponse::Running(mut output) => {
            append_state_banner(&mut output, "running");
            output
        }
        SendResponse::Idle(output) => project_idle(output),
        SendResponse::ReplacementStarting(mut output) => {
            output.push_notice(WORKER_STARTING_STATE);
            output
        }
        SendResponse::ReplacementReady(output) => project_replacement_ready(output),
        SendResponse::Restarted(output) => output,
    }
}

pub(super) fn direct_failure(message: impl Into<String>) -> Response {
    let mut output = Response::default();
    output.push_server_failure(message);
    output
}

fn append_input_banner(output: &mut Response) {
    if output.text_needs_newline() {
        output.push_text("\n");
    }
    output.push_text(render_notice("stdin needed"));
}

fn append_state_banner(output: &mut Response, state: &str) {
    output.push_text("\n");
    output.push_text(render_notice(state));
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
