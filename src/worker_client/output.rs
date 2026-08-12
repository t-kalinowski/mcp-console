#[cfg(target_os = "macos")]
use std::sync::{Arc, Mutex};

#[cfg(target_os = "macos")]
const WORKER_RESTART_BANNER: &str = "[worker restarted: in-memory state lost]\n";

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
    restart_notice: String,
}

#[cfg(target_os = "macos")]
enum CapturedOutputEvent {
    Data { stream: usize, bytes: Vec<u8> },
    Closed { stream: usize },
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
}

impl From<String> for SendFailure {
    fn from(message: String) -> Self {
        Self {
            output: Response::default(),
            message,
        }
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

    fn extend(&mut self, other: Self) {
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
}

impl super::Client {
    pub(super) fn attach_output(&self, result: Result<SendResponse, SendFailure>) -> Response {
        let (captured_output, restart_notice) = self.0.output.take();
        let mut output = Response::default();
        output.push_text(captured_output);
        match result {
            Ok(response) => render_response(output, response, restart_notice),
            Err(SendFailure {
                output: worker_output,
                message,
            }) => {
                output.extend(worker_output);
                if output.is_empty() && restart_notice.is_empty() {
                    output.push_text(message);
                } else {
                    attach_error_output(&mut output, message, restart_notice);
                }
                output.is_error = true;
                output
            }
        }
    }
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

    pub(super) fn push_restart_notice(&self) {
        self.0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .restart_notice
            .push_str(WORKER_RESTART_BANNER);
    }

    fn take(&self) -> (String, String) {
        let mut state = self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let events = std::mem::take(&mut state.events);
        let restart_notice = std::mem::take(&mut state.restart_notice);
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
            }
        }

        (output, restart_notice)
    }
}

#[cfg(not(target_os = "macos"))]
impl CapturedOutput {
    pub(super) fn new() -> Self {
        Self
    }

    pub(super) fn push_restart_notice(&self) {}

    fn take(&self) -> (String, String) {
        (String::new(), String::new())
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

fn render_response(
    mut output: Response,
    response: SendResponse,
    restart_notice: String,
) -> Response {
    match response {
        SendResponse::Completed(completed) => {
            output.extend(completed);
            append_restart_notice(&mut output, &restart_notice);
            if output.is_empty() {
                output.push_text("[done]");
            }
            output
        }
        SendResponse::InputRequested(input) => {
            output.extend(input);
            append_input_banner(&mut output, &restart_notice);
            output
        }
        SendResponse::Running => {
            append_state_banner(&mut output, &restart_notice, "[running]");
            output
        }
        SendResponse::Idle => {
            append_state_banner(&mut output, &restart_notice, "[idle]");
            output
        }
    }
}

fn append_input_banner(output: &mut Response, restart_notice: &str) {
    if !append_restart_notice(output, restart_notice) && output.text_needs_newline() {
        output.push_text("\n");
    }
    output.push_text("[stdin needed]");
}

fn append_state_banner(output: &mut Response, restart_notice: &str, banner: &str) {
    if !append_restart_notice(output, restart_notice) {
        output.push_text("\n");
    }
    output.push_text(banner);
}

fn append_restart_notice(output: &mut Response, restart_notice: &str) -> bool {
    if restart_notice.is_empty() {
        return false;
    }
    if output.text_needs_newline() {
        output.push_text("\n");
    }
    output.push_text(restart_notice);
    true
}

fn attach_error_output(output: &mut Response, error: String, restart_notice: String) {
    if !output.is_empty() && output.text_needs_newline() {
        output.push_text("\n");
    }
    output.push_text(format!("[{error}]"));
    append_restart_notice(output, &restart_notice);
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
