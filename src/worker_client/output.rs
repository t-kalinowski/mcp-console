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
    next_restart_notice: u64,
}

#[cfg(target_os = "macos")]
enum CapturedOutputEvent {
    Data { stream: usize, bytes: Vec<u8> },
    Closed { stream: usize },
    RestartPending { notice: u64 },
    Restarted,
}

#[cfg(target_os = "macos")]
pub(super) struct RestartNotice(u64);

#[cfg(not(target_os = "macos"))]
pub(super) struct RestartNotice;

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
        let (captured_output, restart_notice_at_end) = self.0.output.take();
        let mut output = Response::default();
        output.push_text(captured_output);
        match result {
            Ok(response) => render_response(output, response, restart_notice_at_end),
            Err(SendFailure {
                output: worker_output,
                message,
            }) => {
                output.extend(worker_output);
                if output.is_empty() {
                    output.push_text(message);
                } else {
                    attach_error_output(&mut output, message);
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

    pub(super) fn begin_restart_notice(&self) -> RestartNotice {
        let mut state = self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let notice = state.next_restart_notice;
        state.next_restart_notice += 1;
        state
            .events
            .push(CapturedOutputEvent::RestartPending { notice });
        RestartNotice(notice)
    }

    pub(super) fn commit_restart_notice(&self, notice: RestartNotice) {
        let mut state = self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let event = state
            .events
            .iter_mut()
            .find(|event| {
                matches!(event, CapturedOutputEvent::RestartPending { notice: pending } if *pending == notice.0)
            })
            .expect("pending worker restart notice should exist");
        *event = CapturedOutputEvent::Restarted;
    }

    pub(super) fn cancel_restart_notice(&self, notice: RestartNotice) {
        let mut state = self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let index = state
            .events
            .iter()
            .position(|event| {
                matches!(event, CapturedOutputEvent::RestartPending { notice: pending } if *pending == notice.0)
            })
            .expect("pending worker restart notice should exist");
        state.events.remove(index);
    }

    fn take(&self) -> (String, bool) {
        let mut state = self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let available = state
            .events
            .iter()
            .position(|event| matches!(event, CapturedOutputEvent::RestartPending { .. }))
            .unwrap_or(state.events.len());
        let events = state.events.drain(..available).collect::<Vec<_>>();
        let mut output = String::new();
        let mut restart_notice_at_end = false;

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
                    if !complete.is_empty() {
                        output.push_str(&String::from_utf8_lossy(&complete));
                        restart_notice_at_end = false;
                    }
                }
                CapturedOutputEvent::Closed { stream } => {
                    let pending = state.streams[stream]
                        .take()
                        .expect("captured output stream should be open");
                    if !pending.is_empty() {
                        output.push_str(&String::from_utf8_lossy(&pending));
                        restart_notice_at_end = false;
                    }
                }
                CapturedOutputEvent::Restarted => {
                    if !output.ends_with('\n') {
                        output.push('\n');
                    }
                    output.push_str(WORKER_RESTART_BANNER);
                    restart_notice_at_end = true;
                }
                CapturedOutputEvent::RestartPending { .. } => {
                    unreachable!("pending restart notices are not available")
                }
            }
        }

        (output, restart_notice_at_end)
    }
}

#[cfg(not(target_os = "macos"))]
impl CapturedOutput {
    pub(super) fn new() -> Self {
        Self
    }

    pub(super) fn begin_restart_notice(&self) -> RestartNotice {
        RestartNotice
    }

    pub(super) fn commit_restart_notice(&self, _notice: RestartNotice) {}

    pub(super) fn cancel_restart_notice(&self, _notice: RestartNotice) {}

    fn take(&self) -> (String, bool) {
        (String::new(), false)
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
    restart_notice_at_end: bool,
) -> Response {
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
            append_state_banner(&mut output, restart_notice_at_end, "[running]");
            output
        }
        SendResponse::Idle => {
            append_state_banner(&mut output, restart_notice_at_end, "[idle]");
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

fn append_state_banner(output: &mut Response, restart_notice_at_end: bool, banner: &str) {
    if !restart_notice_at_end {
        output.push_text("\n");
    }
    output.push_text(banner);
}

fn attach_error_output(output: &mut Response, error: String) {
    if !output.is_empty() && output.text_needs_newline() {
        output.push_text("\n");
    }
    output.push_text(format!("[{error}]"));
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
