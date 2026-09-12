//! Versioned target bootstrap and envelope around unchanged relay JSONL.
use crate::ssh::preparation;
use serde::{Deserialize, Serialize};
use std::io::{self, Read, Write};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

#[cfg(unix)]
mod launch;
#[cfg(unix)]
pub(crate) mod transfer;

pub(crate) const VERSION: u32 = 2;
pub(crate) const MAX_BOOTSTRAP: usize = 1024 * 1024;
pub(crate) const MAX_FRAME: usize = 64 * 1024;
pub(crate) const HELLO: u8 = 1;
pub(crate) const DATA: u8 = 2;
pub(crate) const RETIRED: u8 = 3;
pub(crate) const SETUP_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(30);
#[derive(Clone, Copy)]
pub(crate) struct Protocol(pub &'static str);

pub(crate) fn encode(bootstrap: &impl Serialize) -> Result<Vec<u8>, String> {
    let payload = serde_json::to_vec(bootstrap).map_err(|error| error.to_string())?;
    if payload.len() > MAX_BOOTSTRAP {
        return Err("target bootstrap exceeds 1 MiB".into());
    }
    let mut bytes = (payload.len() as u32).to_be_bytes().to_vec();
    bytes.extend(payload);
    Ok(bytes)
}

pub(crate) fn run(protocol: Protocol, probe: bool, container: bool) -> Result<(), String> {
    #[cfg(unix)]
    return launch::run(protocol, probe, container);
    #[cfg(not(unix))]
    Err("target execution requires macOS or Linux".into())
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Bootstrap {
    pub version: u32,
    pub build: String,
    pub workspace: String,
    pub policy: crate::settings::SandboxSettings,
    pub writable_roots: Vec<PathBuf>,
    pub no_sandbox: bool,
    #[serde(default)]
    pub environment: Option<preparation::WorkerEnvironment>,
}

pub(crate) fn enter_workspace(workspace: &str) -> Result<(), String> {
    if !workspace.starts_with('/') {
        return Err("target.workspace must be an absolute remote directory path".into());
    }
    let metadata = std::fs::metadata(workspace)
        .map_err(|error| format!("cannot access remote target.workspace '{workspace}': {error}"))?;
    if !metadata.is_dir() {
        return Err(format!(
            "remote target.workspace '{workspace}' is not a directory"
        ));
    }
    std::env::set_current_dir(workspace)
        .map_err(|error| format!("cannot enter remote target.workspace: {error}"))
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Hello {
    pub version: u32,
    pub build: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub container_id: Option<String>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Retired {
    pub confirmed: bool,
    pub error: Option<String>,
}

#[derive(Clone, Default)]
pub(crate) struct Retirement(Arc<Mutex<bool>>);

impl Retirement {
    pub fn check(&self) -> Result<(), String> {
        if *self
            .0
            .lock()
            .map_err(|_| "target retirement lock poisoned")?
        {
            Ok(())
        } else {
            Err("target retirement is unconfirmed (transport exit is not a cleanup barrier)".into())
        }
    }
}

pub(crate) struct Output<R> {
    reader: R,
    protocol: Protocol,
    retirement: Retirement,
    hello: bool,
    finished: bool,
    pending: io::Cursor<Vec<u8>>,
    recording: Option<crate::transcript::Transcript>,
}

impl<R: Read> Output<R> {
    pub fn new(reader: R, protocol: Protocol, retirement: Retirement) -> Self {
        Self {
            reader,
            protocol,
            retirement,
            hello: false,
            finished: false,
            pending: io::Cursor::new(Vec::new()),
            recording: None,
        }
    }

    pub fn with_recording(mut self, recording: Option<crate::transcript::Transcript>) -> Self {
        self.recording = recording;
        self
    }

    fn next_frame(&mut self) -> io::Result<bool> {
        let mut tag = [0];
        self.reader.read_exact(&mut tag).map_err(|error| {
            io::Error::other(format!(
                "{} launch stream ended before confirmed retirement: {error}",
                self.protocol.0
            ))
        })?;
        if !matches!(tag[0], HELLO | DATA | RETIRED) {
            return Err(io::Error::other(format!(
                "unexpected stdout in {} launch protocol",
                self.protocol.0
            )));
        }
        let bytes = read_payload(&mut self.reader, MAX_FRAME, self.protocol)?;
        match tag[0] {
            HELLO if !self.hello => {
                let hello: Hello = serde_json::from_slice(&bytes).map_err(io::Error::other)?;
                self.protocol
                    .compatible(hello.version, &hello.build)
                    .map_err(io::Error::other)?;
                if let (Some(recording), Some(id)) = (&self.recording, &hello.container_id) {
                    recording.target_generation(id);
                }
                self.hello = true;
            }
            DATA if self.hello => {
                self.pending = io::Cursor::new(bytes);
            }
            RETIRED => {
                let retired: Retired = serde_json::from_slice(&bytes).map_err(io::Error::other)?;
                if self.reader.read(&mut tag)? != 0 {
                    return Err(io::Error::other(format!(
                        "unexpected stdout after {} retirement",
                        self.protocol.0
                    )));
                }
                *self
                    .retirement
                    .0
                    .lock()
                    .expect("SSH retirement lock is not poisoned") = retired.confirmed;
                self.finished = true;
                // The adapter validates its cleanup receipt. An outer owned
                // container can retire descendants after its inner launcher
                // fails; its own removal remains the generation barrier.
                if let Some(error) = retired.error {
                    return Err(io::Error::other(error));
                }
                return Ok(false);
            }
            _ => {
                return Err(io::Error::other(format!(
                    "unexpected stdout in {} launch protocol",
                    self.protocol.0
                )));
            }
        }
        Ok(true)
    }
}

impl<R: Read> Read for Output<R> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        if buffer.is_empty() {
            return Ok(0);
        }
        loop {
            let count = self.pending.read(buffer)?;
            if count != 0 || self.finished {
                return Ok(count);
            }
            if !self.next_frame()? {
                return Ok(0);
            }
        }
    }
}

impl Protocol {
    pub fn compatible(&self, version: u32, build: &str) -> Result<(), String> {
        if version != VERSION || build != env!("CARGO_PKG_VERSION") {
            return Err(format!(
                "incompatible {} bootstrap: expected protocol {VERSION}, Console {}; received protocol {version}, Console {build}",
                self.0,
                env!("CARGO_PKG_VERSION")
            ));
        }
        Ok(())
    }
}

pub(crate) fn read_payload(
    reader: &mut impl Read,
    maximum: usize,
    protocol: Protocol,
) -> io::Result<Vec<u8>> {
    let mut length = [0; 4];
    reader.read_exact(&mut length).map_err(|error| {
        io::Error::other(format!("truncated {} frame length: {error}", protocol.0))
    })?;
    let length = u32::from_be_bytes(length) as usize;
    if length > maximum {
        return Err(io::Error::other(format!(
            "{} frame exceeds {maximum} bytes",
            protocol.0
        )));
    }
    let mut bytes = vec![0; length];
    reader.read_exact(&mut bytes).map_err(|error| {
        io::Error::other(format!("truncated {} frame payload: {error}", protocol.0))
    })?;
    Ok(bytes)
}

pub(crate) fn write_frame(writer: &mut impl Write, tag: u8, bytes: &[u8]) -> io::Result<()> {
    writer.write_all(&[tag])?;
    writer.write_all(&(bytes.len() as u32).to_be_bytes())?;
    writer.write_all(bytes)?;
    writer.flush()
}
