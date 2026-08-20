#[cfg(target_os = "macos")]
use std::io::{self, BufRead, Write};

#[cfg(target_os = "macos")]
use base64::Engine as _;
#[cfg(target_os = "macos")]
use serde::{Deserialize, Serialize};

#[cfg(target_os = "macos")]
use crate::worker_protocol::{ServerMessage, WorkerMessage};

#[cfg(target_os = "macos")]
#[derive(Clone, Deserialize, Serialize)]
#[serde(transparent)]
pub(crate) struct EncodedBytes(String);

#[cfg(target_os = "macos")]
#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum RelayCommand {
    WorkerMessage { message: ServerMessage },
    Stdin { data: EncodedBytes },
    Interrupt { request_id: u64 },
    Shutdown { grace_millis: u64 },
    Acknowledge { sequence: u64 },
}

#[cfg(target_os = "macos")]
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RelayEvent {
    pub(crate) sequence: u64,
    pub(crate) payload: RelayEventPayload,
}

#[cfg(target_os = "macos")]
#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum RelayEventPayload {
    WorkerMessage {
        message: WorkerMessage,
    },
    Stdout {
        data: EncodedBytes,
    },
    Stderr {
        data: EncodedBytes,
    },
    StreamClosed {
        stream: RelayStream,
    },
    WorkerSidebandClosed,
    InterruptResult {
        request_id: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        error: Option<String>,
    },
    ShutdownStarted,
    WorkerExited {
        status: RelayExitStatus,
    },
    Fatal {
        message: String,
    },
}

#[cfg(target_os = "macos")]
#[derive(Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum RelayStream {
    Stdout,
    Stderr,
}

#[cfg(target_os = "macos")]
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RelayExitStatus {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) code: Option<i32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) signal: Option<i32>,
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
