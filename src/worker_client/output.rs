use std::sync::{Arc, Mutex};

pub(super) const WORKER_STARTING_NOTICE: &str = "[starting new worker]\n";
pub(super) const WORKER_STOPPED_NOTICE: &str = "[worker stopped: in-memory state lost]";

/// Stores pending session output in publication order until one response drains it.
#[derive(Clone)]
pub(super) struct OutputTape(Arc<Mutex<OutputTapeState>>);

#[derive(Default)]
struct OutputTapeState {
    streams: Vec<Option<Vec<u8>>>,
    events: Vec<OutputEvent>,
}

enum OutputEvent {
    StreamData {
        stream: usize,
        bytes: Vec<u8>,
    },
    StreamClosed {
        stream: usize,
    },
    Text(String),
    Image {
        data: String,
        mime_type: String,
        artifact: crate::transcript::Artifact,
    },
    Line(String),
    Failure(SendFailure),
}

#[cfg(target_os = "macos")]
pub(super) struct OutputTapeStream {
    output: OutputTape,
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
    Idle(Response),
    Running(Response),
    InputRequested(Response),
    Completed(Response),
    Restarted,
}

pub(super) struct SendFailure {
    pub(super) message: String,
    pub(super) worker_stopped: bool,
}

impl From<String> for SendFailure {
    fn from(message: String) -> Self {
        Self {
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

    pub(super) fn is_empty(&self) -> bool {
        self.content.is_empty()
    }

    pub(super) fn is_error(&self) -> bool {
        self.is_error
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

impl OutputTape {
    pub(super) fn new() -> Self {
        Self(Arc::new(Mutex::new(OutputTapeState::default())))
    }

    #[cfg(target_os = "macos")]
    pub(super) fn stream(&self) -> OutputTapeStream {
        let mut state = self.lock();
        let stream = state.streams.len();
        state.streams.push(Some(Vec::new()));
        OutputTapeStream {
            output: self.clone(),
            stream,
        }
    }

    pub(super) fn push_text(&self, text: impl Into<String>) {
        let text = text.into();
        if !text.is_empty() {
            self.lock().events.push(OutputEvent::Text(text));
        }
    }

    pub(super) fn push_image(
        &self,
        data: String,
        mime_type: String,
        artifact: crate::transcript::Artifact,
    ) {
        self.lock().events.push(OutputEvent::Image {
            data,
            mime_type,
            artifact,
        });
    }

    pub(super) fn push_line(&self, line: impl Into<String>) {
        self.lock().events.push(OutputEvent::Line(line.into()));
    }

    pub(super) fn push_failure(&self, failure: SendFailure) {
        self.lock().events.push(OutputEvent::Failure(failure));
    }

    pub(super) fn take(&self) -> Response {
        let mut state = self.lock();
        let events = std::mem::take(&mut state.events);
        let mut output = Response::default();

        for event in events {
            match event {
                OutputEvent::StreamData { stream, bytes } => {
                    let pending = state.streams[stream]
                        .as_mut()
                        .expect("output tape stream should be open");
                    pending.extend_from_slice(&bytes);
                    let complete = complete_utf8_prefix(pending);
                    let incomplete = pending.split_off(complete);
                    let complete = std::mem::replace(pending, incomplete);
                    output.push_text(String::from_utf8_lossy(&complete));
                }
                OutputEvent::StreamClosed { stream } => {
                    let pending = state.streams[stream]
                        .take()
                        .expect("output tape stream should be open");
                    output.push_text(String::from_utf8_lossy(&pending));
                }
                OutputEvent::Text(text) => output.push_text(text),
                OutputEvent::Image {
                    data,
                    mime_type,
                    artifact,
                } => output.push_image(data, mime_type, artifact),
                OutputEvent::Line(line) => output.push_line(line),
                OutputEvent::Failure(SendFailure {
                    message,
                    worker_stopped,
                }) => {
                    if output.is_empty() && !worker_stopped {
                        output.push_text(message);
                    } else {
                        output.push_line(format!("[{message}]"));
                    }
                    if worker_stopped {
                        output.push_line(WORKER_STOPPED_NOTICE);
                    }
                    output.is_error = true;
                }
            }
        }

        output
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, OutputTapeState> {
        self.0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

#[cfg(target_os = "macos")]
impl OutputTapeStream {
    pub(super) fn push(&self, bytes: &[u8]) {
        self.output.lock().events.push(OutputEvent::StreamData {
            stream: self.stream,
            bytes: bytes.to_vec(),
        });
    }

    pub(super) fn close(&self) {
        self.output.lock().events.push(OutputEvent::StreamClosed {
            stream: self.stream,
        });
    }
}

pub(super) fn render_response(response: SendResponse) -> Response {
    match response {
        SendResponse::Completed(mut output) => {
            if output.is_empty() {
                output.push_text("[done]");
            }
            output
        }
        SendResponse::InputRequested(mut output) => {
            append_input_banner(&mut output);
            output
        }
        SendResponse::Running(mut output) => {
            append_state_banner(&mut output, "[running]");
            output
        }
        SendResponse::Idle(mut output) => {
            if !output.is_error() {
                append_state_banner(&mut output, "[idle]");
            }
            output
        }
        SendResponse::Restarted => {
            direct_failure("session restarted before the operation completed")
        }
    }
}

pub(super) fn direct_failure(message: impl Into<String>) -> Response {
    let mut output = Response::default();
    output.push_text(message);
    output.is_error = true;
    output
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
