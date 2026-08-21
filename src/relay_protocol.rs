#[cfg(target_os = "macos")]
use std::io::{self, BufRead, Write};

#[cfg(target_os = "macos")]
use base64::Engine as _;
#[cfg(target_os = "macos")]
use serde::{Deserialize, Serialize};

#[cfg(target_os = "macos")]
use crate::cell::Language;
#[cfg(target_os = "macos")]
use crate::worker_protocol::{
    PythonRequirementManifest, PythonResolveRequest, PythonVersionResolveRequest, WorkerMessage,
};

#[cfg(target_os = "macos")]
#[derive(Clone, Deserialize, Serialize)]
#[serde(transparent)]
pub(crate) struct EncodedBytes(String);

#[cfg(target_os = "macos")]
#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum RelayCommand {
    Evaluate { language: Language, source: String },
    PrepareR { library: String },
    PreparePython { packages: Vec<String> },
    PythonResolved { python: String },
    PythonResolutionFailed { message: String },
    PythonVersionResolved { version: String },
    PythonVersionResolutionFailed { message: String },
    Stdin { data: String },
    Interrupt { request_id: u64 },
    Shutdown { grace_millis: u64 },
}

#[cfg(target_os = "macos")]
#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum RelayEvent {
    Ready,
    ConsoleOutput {
        data: String,
    },
    ConsoleDiagnostic {
        data: String,
    },
    Image {
        data: String,
        mime_type: String,
    },
    InputRequested {
        prompt: String,
    },
    InputReceived,
    InputCancelled,
    RPrepared {
        library: String,
    },
    RPreparationFailed {
        message: String,
    },
    ResolvePython {
        request: PythonResolveRequest,
    },
    ResolvePythonVersion {
        request: PythonVersionResolveRequest,
    },
    PythonActivated {
        requirements: PythonRequirementManifest,
    },
    PythonPrepared,
    PythonPreparationFailed {
        message: String,
    },
    Completed,
    Stdout {
        data: String,
    },
    Stderr {
        data: String,
    },
    StdoutBytes {
        data: EncodedBytes,
    },
    StderrBytes {
        data: EncodedBytes,
    },
    StdoutClosed,
    StderrClosed,
    WorkerSidebandClosed,
    InterruptResult {
        request_id: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        error: Option<String>,
    },
    ShutdownStarted,
    WorkerExited {
        code: i32,
    },
    WorkerSignaled {
        signal: i32,
    },
    Fatal {
        message: String,
    },
}

#[cfg(target_os = "macos")]
impl From<WorkerMessage> for RelayEvent {
    fn from(message: WorkerMessage) -> Self {
        match message {
            WorkerMessage::Ready => Self::Ready,
            WorkerMessage::ConsoleOutput { data } => Self::ConsoleOutput { data },
            WorkerMessage::ConsoleDiagnostic { data } => Self::ConsoleDiagnostic { data },
            WorkerMessage::Image { data, mime_type } => Self::Image { data, mime_type },
            WorkerMessage::InputRequested { prompt } => Self::InputRequested { prompt },
            WorkerMessage::InputReceived => Self::InputReceived,
            WorkerMessage::InputCancelled => Self::InputCancelled,
            WorkerMessage::RPrepared { library } => Self::RPrepared { library },
            WorkerMessage::RPreparationFailed { message } => Self::RPreparationFailed { message },
            WorkerMessage::ResolvePython { request } => Self::ResolvePython { request },
            WorkerMessage::ResolvePythonVersion { request } => {
                Self::ResolvePythonVersion { request }
            }
            WorkerMessage::PythonActivated { requirements } => {
                Self::PythonActivated { requirements }
            }
            WorkerMessage::PythonPrepared => Self::PythonPrepared,
            WorkerMessage::PythonPreparationFailed { message } => {
                Self::PythonPreparationFailed { message }
            }
            WorkerMessage::Completed => Self::Completed,
        }
    }
}

#[cfg(target_os = "macos")]
impl EncodedBytes {
    pub(crate) fn from_bytes(bytes: &[u8]) -> Self {
        Self(base64::engine::general_purpose::STANDARD.encode(bytes))
    }

    pub(crate) fn decode(&self) -> Result<Vec<u8>, String> {
        base64::engine::general_purpose::STANDARD
            .decode(&self.0)
            .map_err(|error| format!("relay received invalid base64 data: {error}"))
    }
}

#[cfg(target_os = "macos")]
pub(crate) struct JsonlReader<R> {
    reader: R,
    buffer: Vec<u8>,
}

#[cfg(target_os = "macos")]
impl<R: BufRead> JsonlReader<R> {
    pub(crate) fn new(reader: R) -> Self {
        Self {
            reader,
            buffer: Vec::new(),
        }
    }

    pub(crate) fn receive<T: serde::de::DeserializeOwned>(&mut self) -> io::Result<Option<T>> {
        self.buffer.clear();
        let length = self.reader.read_until(b'\n', &mut self.buffer)?;
        if length == 0 {
            return Ok(None);
        }
        if self.buffer.last() != Some(&b'\n') {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "relay stream closed midway through a frame",
            ));
        }
        self.buffer.pop();
        if self.buffer.last() == Some(&b'\r') {
            self.buffer.pop();
        }
        serde_json::from_slice(&self.buffer)
            .map(Some)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
    }
}

#[cfg(target_os = "macos")]
pub(crate) struct JsonlWriter<W> {
    writer: W,
}

#[cfg(target_os = "macos")]
impl<W: Write> JsonlWriter<W> {
    pub(crate) fn new(writer: W) -> Self {
        Self { writer }
    }

    pub(crate) fn send<T: Serialize>(&mut self, message: &T) -> io::Result<()> {
        serde_json::to_writer(&mut self.writer, message)?;
        self.writer.write_all(b"\n")?;
        self.writer.flush()
    }
}
