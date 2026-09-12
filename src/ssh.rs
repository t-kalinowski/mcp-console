//! Authenticated launch framing around the unchanged relay JSONL stream.

use std::io::{self, Read, Write};
use std::path::PathBuf;
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::{Deserialize, Serialize};

#[cfg(unix)]
mod launch;
#[cfg(unix)]
mod launch_io;
pub(crate) mod preparation;

pub(crate) const SETUP_TIMEOUT: Duration = Duration::from_secs(30);
const VERSION: u32 = 2;
const MAX_BOOTSTRAP: usize = 1024 * 1024;
const MAX_FRAME: usize = 64 * 1024;
const HELLO: u8 = 1;
const DATA: u8 = 2;
const RETIRED: u8 = 3;

#[derive(Clone)]
pub(crate) struct Session {
    pub target: crate::settings::SshTarget,
    roots: Vec<PathBuf>,
    blocked: Arc<Mutex<Option<String>>>,
    pub preparation: Option<preparation::Preparation>,
    discovery: Option<preparation::Discovery>,
}

impl Session {
    pub fn new(target: crate::settings::SshTarget, roots: Vec<PathBuf>) -> Self {
        Self {
            target,
            roots,
            blocked: Arc::default(),
            preparation: None,
            discovery: None,
        }
    }

    pub fn metadata(&self) -> serde_json::Value {
        serde_json::json!({"transport": self.target.transport, "workspace": self.target.workspace})
    }

    pub fn command(&self) -> Result<Command, String> {
        self.command_for("ssh-launch")
    }

    fn command_for(&self, operation: &str) -> Result<Command, String> {
        if let Some(error) = &*self
            .blocked
            .lock()
            .map_err(|_| "SSH session lock poisoned")?
        {
            return Err(error.clone());
        }
        // OpenSSH joins remote argv with spaces and passes it through a shell.
        // Only the trusted executable prefix goes there; all launch data uses stdin.
        let remote = self
            .target
            .command
            .iter()
            .map(|argument| format!("'{}'", argument.replace('\'', "'\\''")))
            .chain(std::iter::once(format!("'{operation}'")))
            .collect::<Vec<_>>()
            .join(" ");
        let mut command = Command::new("ssh");
        command.args([
            "-T",
            "-a",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPersist=no",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "PermitLocalCommand=no",
            "--",
            self.target.host(),
            &remote,
        ]);
        Ok(command)
    }

    pub fn bootstrap(
        &self,
        policy: &crate::settings::SandboxSettings,
        no_sandbox: bool,
        managed_r: Option<&crate::resolver::ManagedR>,
        python: Option<&crate::resolver::ManagedPython>,
    ) -> Result<Vec<u8>, String> {
        let value = serde_json::to_vec(&Bootstrap {
            version: VERSION,
            build: env!("CARGO_PKG_VERSION").into(),
            workspace: self.target.workspace.clone(),
            policy: policy.clone(),
            writable_roots: self.roots.clone(),
            no_sandbox,
            environment: self
                .discovery
                .clone()
                .map(|discovery| preparation::WorkerEnvironment {
                    discovery,
                    r: managed_r.cloned(),
                    python: python.cloned(),
                }),
        })
        .map_err(|error| format!("cannot encode SSH bootstrap: {error}"))?;
        if value.len() > MAX_BOOTSTRAP {
            return Err("SSH bootstrap exceeds 1 MiB".into());
        }
        let mut bytes = (value.len() as u32).to_be_bytes().to_vec();
        bytes.extend(value);
        Ok(bytes)
    }

    pub fn discover(
        &mut self,
        policy: &crate::settings::SandboxSettings,
        on_started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<preparation::Discovery, String> {
        let selections = preparation::Selections::from_policy(policy);
        let (preparation, discovery) =
            preparation::Preparation::open(self, selections, on_started)?;
        self.preparation = Some(preparation);
        self.discovery = Some(discovery.clone());
        Ok(discovery)
    }

    fn block(&self) {
        let mut blocked = self
            .blocked
            .lock()
            .expect("SSH session lock is not poisoned");
        blocked.get_or_insert_with(|| format!(
            "SSH target '{}' retirement is unconfirmed; this session cannot start a replacement",
            self.target.host(),
        ));
    }
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Bootstrap {
    version: u32,
    build: String,
    workspace: String,
    policy: crate::settings::SandboxSettings,
    writable_roots: Vec<PathBuf>,
    no_sandbox: bool,
    #[serde(default)]
    environment: Option<preparation::WorkerEnvironment>,
}

fn enter_workspace(workspace: &str) -> Result<(), String> {
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
struct Hello {
    version: u32,
    build: String,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Retired {
    confirmed: bool,
    error: Option<String>,
}

#[derive(Clone, Default)]
pub(crate) struct Retirement(Arc<Mutex<bool>>);

impl Retirement {
    pub fn check(&self, session: &Session) -> Result<(), String> {
        if *self.0.lock().map_err(|_| "SSH retirement lock poisoned")? {
            Ok(())
        } else {
            session.block();
            Err("remote retirement is unconfirmed (SSH exit is not a cleanup barrier)".into())
        }
    }
}

pub(crate) struct Output<R> {
    reader: R,
    session: Session,
    retirement: Retirement,
    hello: bool,
    finished: bool,
    pending: io::Cursor<Vec<u8>>,
}

impl<R: Read> Output<R> {
    pub fn new(reader: R, session: Session, retirement: Retirement) -> Self {
        Self {
            reader,
            session,
            retirement,
            hello: false,
            finished: false,
            pending: io::Cursor::new(Vec::new()),
        }
    }

    fn next_frame(&mut self) -> io::Result<bool> {
        let mut tag = [0];
        self.reader.read_exact(&mut tag).map_err(|error| {
            io::Error::other(format!(
                "SSH launch stream ended before confirmed retirement: {error}"
            ))
        })?;
        if !matches!(tag[0], HELLO | DATA | RETIRED) {
            return Err(io::Error::other("unexpected stdout in SSH launch protocol"));
        }
        let bytes = read_payload(&mut self.reader, MAX_FRAME)?;
        match tag[0] {
            HELLO if !self.hello => {
                let hello: Hello = serde_json::from_slice(&bytes).map_err(io::Error::other)?;
                compatible(hello.version, &hello.build).map_err(io::Error::other)?;
                self.hello = true;
            }
            DATA if self.hello => {
                self.pending = io::Cursor::new(bytes);
            }
            RETIRED => {
                let retired: Retired = serde_json::from_slice(&bytes).map_err(io::Error::other)?;
                if self.reader.read(&mut tag)? != 0 {
                    return Err(io::Error::other("unexpected stdout after SSH retirement"));
                }
                *self
                    .retirement
                    .0
                    .lock()
                    .expect("SSH retirement lock is not poisoned") = retired.confirmed;
                self.finished = true;
                let cleanup = self.retirement.check(&self.session);
                match (retired.error, cleanup) {
                    (Some(error), Err(cleanup)) => {
                        return Err(io::Error::other(format!("{error}; {cleanup}")));
                    }
                    (Some(error), Ok(())) | (None, Err(error)) => {
                        return Err(io::Error::other(error));
                    }
                    (None, Ok(())) => {}
                }
                return Ok(false);
            }
            _ => return Err(io::Error::other("unexpected stdout in SSH launch protocol")),
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

fn compatible(version: u32, build: &str) -> Result<(), String> {
    if version != VERSION || build != env!("CARGO_PKG_VERSION") {
        return Err(format!(
            "incompatible SSH bootstrap: expected protocol {VERSION}, Console {}; received protocol {version}, Console {build}",
            env!("CARGO_PKG_VERSION")
        ));
    }
    Ok(())
}

fn read_payload(reader: &mut impl Read, maximum: usize) -> io::Result<Vec<u8>> {
    let mut length = [0; 4];
    reader
        .read_exact(&mut length)
        .map_err(|error| io::Error::other(format!("truncated SSH frame length: {error}")))?;
    let length = u32::from_be_bytes(length) as usize;
    if length > maximum {
        return Err(io::Error::other(format!(
            "SSH frame exceeds {maximum} bytes"
        )));
    }
    let mut bytes = vec![0; length];
    reader
        .read_exact(&mut bytes)
        .map_err(|error| io::Error::other(format!("truncated SSH frame payload: {error}")))?;
    Ok(bytes)
}

fn write_frame(writer: &mut impl Write, tag: u8, bytes: &[u8]) -> io::Result<()> {
    writer.write_all(&[tag])?;
    writer.write_all(&(bytes.len() as u32).to_be_bytes())?;
    writer.write_all(bytes)?;
    writer.flush()
}

pub(crate) fn run() -> Result<(), String> {
    #[cfg(unix)]
    return launch::run();
    #[cfg(not(unix))]
    Err("SSH execution requires macOS or Linux".into())
}
