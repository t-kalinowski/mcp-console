#[cfg(target_os = "macos")]
use std::sync::{Arc, Mutex};

pub(super) const WORKER_STARTED_NOTICE: &str = "[starting new worker]\n";
pub(super) const WORKER_STOPPED_NOTICE: &str = "[worker stopped: in-memory state lost]";

#[cfg(target_os = "macos")]
#[derive(Clone)]
pub(super) struct CapturedOutput(Arc<Mutex<CapturedOutputState>>);

#[cfg(not(target_os = "macos"))]
#[derive(Clone)]
pub(super) struct CapturedOutput;

#[cfg(target_os = "macos")]
#[derive(Default)]
struct CapturedOutputState {
    streams: Vec<Option<Vec<u8>>>,
    events: Vec<CapturedOutputEvent>,
}

#[cfg(target_os = "macos")]
enum CapturedOutputEvent {
    Data { stream: usize, bytes: Vec<u8> },
    Closed { stream: usize },
    Notice(String),
}

#[cfg(target_os = "macos")]
pub(super) struct CapturedOutputStream {
    output: CapturedOutput,
    stream: usize,
}

#[derive(Default)]
pub(crate) struct Response {
    content: Vec<Content>,
    is_error: bool,
}

pub(crate) enum Content {
    Text(String),
    Image {
        data: String,
        mime_type: String,
        artifact: crate::transcript::Artifact,
    },
}

pub(super) enum SendResponse {
    Idle,
    Running,
    InputRequested(Response),
    Completed(Response),
}

pub(super) struct SendFailure {
    pub(super) output: Response,
    pub(super) message: String,
    pub(super) worker_stopped: bool,
}

impl From<String> for SendFailure {
    fn from(message: String) -> Self {
        Self {
            output: Response::default(),
            message,
            worker_stopped: false,
        }
    }
}

impl SendFailure {
    pub(super) fn worker_stopped(mut self) -> Self {
        self.worker_stopped = true;
        self
    }
}

impl Response {
    pub(crate) fn into_parts(self) -> (Vec<Content>, bool) {
        (self.content, self.is_error)
    }

    pub(super) fn push_text(&mut self, text: impl Into<String>) {
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

    pub(super) fn push_image(
        &mut self,
        data: String,
        mime_type: String,
        artifact: crate::transcript::Artifact,
    ) {
        self.content.push(Content::Image {
            data,
            mime_type,
            artifact,
        });
    }

    pub(super) fn extend(&mut self, other: Self) {
        for content in other.content {
            match content {
                Content::Text(text) => self.push_text(text),
                Content::Image {
                    data,
                    mime_type,
                    artifact,
                } => self.push_image(data, mime_type, artifact),
            }
        }
    }

    pub(super) fn is_empty(&self) -> bool {
        self.content.is_empty()
    }

    pub(super) fn text_needs_newline(&self) -> bool {
        !matches!(self.content.last(), Some(Content::Text(text)) if text.ends_with('\n'))
    }

    pub(super) fn push_line(&mut self, text: impl Into<String>) {
        if !self.is_empty() && self.text_needs_newline() {
            self.push_text("\n");
        }
        self.push_text(text);
    }
}

impl super::Client {
    pub(super) fn attach_output(&self, result: Result<SendResponse, SendFailure>) -> Response {
        let lifecycle = self.0.lifecycle.lock();
        let captured_output = if lifecycle
            .as_ref()
            .is_ok_and(|lifecycle| lifecycle.state == super::lifecycle::LifecycleState::Ready)
        {
            self.0.output.take()
        } else {
            String::new()
        };
        drop(lifecycle);
        assemble_response(captured_output, result)
    }

    pub(crate) fn worker_stopped_response(&self, message: String) -> Response {
        self.attach_output(Err(SendFailure::from(message).worker_stopped()))
    }
}

fn assemble_response(
    captured_output: String,
    result: Result<SendResponse, SendFailure>,
) -> Response {
    let mut output = Response::default();
    output.push_text(captured_output);
    match result {
        Ok(response) => render_response(output, response),
        Err(SendFailure {
            output: worker_output,
            message,
            worker_stopped,
        }) => {
            output.extend(worker_output);
            if output.is_empty() && !worker_stopped {
                output.push_text(message);
            } else {
                attach_error_output(&mut output, message, worker_stopped);
            }
            output.is_error = true;
            output
        }
    }
}

pub(super) fn render_failure(captured_output: String, failure: SendFailure) -> Response {
    assemble_response(captured_output, Err(failure))
}

#[cfg(target_os = "macos")]
impl CapturedOutput {
    pub(super) fn new() -> Self {
        Self(Arc::new(Mutex::new(CapturedOutputState::default())))
    }

    pub(super) fn stream(&self) -> CapturedOutputStream {
        let mut state = self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let stream = state.streams.len();
        state.streams.push(Some(Vec::new()));
        CapturedOutputStream {
            output: self.clone(),
            stream,
        }
    }

    pub(super) fn push_notice(&self, notice: impl Into<String>) {
        self.0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .events
            .push(CapturedOutputEvent::Notice(notice.into()));
    }

    pub(super) fn take(&self) -> String {
        let mut state = self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let events = std::mem::take(&mut state.events);
        let mut output = String::new();

        for event in events {
            match event {
                CapturedOutputEvent::Data { stream, bytes } => {
                    let pending = state.streams[stream]
                        .as_mut()
                        .expect("captured output stream should be open");
                    pending.extend_from_slice(&bytes);
                    let complete = complete_utf8_prefix(pending);
                    let incomplete = pending.split_off(complete);
                    let complete = std::mem::replace(pending, incomplete);
                    output.push_str(&String::from_utf8_lossy(&complete));
                }
                CapturedOutputEvent::Closed { stream } => {
                    let pending = state.streams[stream]
                        .take()
                        .expect("captured output stream should be open");
                    output.push_str(&String::from_utf8_lossy(&pending));
                }
                CapturedOutputEvent::Notice(notice) => {
                    if !output.is_empty() && !output.ends_with('\n') {
                        output.push('\n');
                    }
                    output.push_str(&notice);
                }
            }
        }

        output
    }
}

#[cfg(not(target_os = "macos"))]
impl CapturedOutput {
    pub(super) fn new() -> Self {
        Self
    }

    pub(super) fn push_notice(&self, _notice: impl Into<String>) {}

    pub(super) fn take(&self) -> String {
        String::new()
    }
}

#[cfg(target_os = "macos")]
impl CapturedOutputStream {
    pub(super) fn push(&self, bytes: &[u8]) {
        self.output
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .events
            .push(CapturedOutputEvent::Data {
                stream: self.stream,
                bytes: bytes.to_vec(),
            });
    }

    pub(super) fn close(&self) {
        self.output
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .events
            .push(CapturedOutputEvent::Closed {
                stream: self.stream,
            });
    }
}

fn render_response(mut output: Response, response: SendResponse) -> Response {
    match response {
        SendResponse::Completed(completed) => {
            output.extend(completed);
            if output.is_empty() {
                output.push_text("[done]");
            }
            output
        }
        SendResponse::InputRequested(input) => {
            output.extend(input);
            append_input_banner(&mut output);
            output
        }
        SendResponse::Running => {
            append_state_banner(&mut output, "[running]");
            output
        }
        SendResponse::Idle => {
            append_state_banner(&mut output, "[idle]");
            output
        }
    }
}

fn append_input_banner(output: &mut Response) {
    if output.text_needs_newline() {
        output.push_text("\n");
    }
    output.push_text("[stdin needed]");
}

fn append_state_banner(output: &mut Response, banner: &str) {
    output.push_text("\n");
    output.push_text(banner);
}

fn attach_error_output(output: &mut Response, error: String, worker_stopped: bool) {
    output.push_line(format!("[{error}]"));
    if worker_stopped {
        output.push_line(WORKER_STOPPED_NOTICE);
    }
}

#[cfg(target_os = "macos")]
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
