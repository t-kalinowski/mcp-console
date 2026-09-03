use std::ffi::OsStr;
use std::io::{self, Write as _};
use std::os::fd::{AsRawFd as _, RawFd};
use std::os::unix::net::UnixStream;
use std::process::{Child, Command, Stdio};

use super::{TARGET_GATE_RELEASE, platform, supervision};

/// A command configured to run under the macOS sandbox.
pub(crate) struct SandboxedCommand {
    pub(super) command: Command,
    pub(super) temporary_directory: platform::TemporaryDirectory,
    pub(super) startup_gate: StartupGate,
}

pub(super) struct StartupGate {
    target: Option<UnixStream>,
    owner: UnixStream,
}

pub(super) struct ManagedRoot {
    pub(super) child: Child,
    pub(super) supervisor: supervision::SandboxManager,
}

/// A direct sandboxed child and its host-owned lifetime manager.
///
/// Retain this owner until retirement, then call `force_stop` to retire the
/// sandbox root and observed descendants and reap its direct process. Dropping
/// this owner runs the same path on a best-effort basis, keeping sandbox-lifetime
/// cleanup before direct-process reaping. Piped streams can be taken and moved
/// to independent I/O tasks before retirement.
#[must_use = "retain the sandboxed child until it is explicitly retired"]
pub(crate) struct SandboxedChild {
    pub(super) root: ManagedRoot,
    pub(super) retirement: SandboxedChildRetirement,
}

pub(super) enum SandboxedChildRetirement {
    Managed,
    Unmanaged { error: String },
    Retired { error: Option<String> },
}

impl StartupGate {
    fn new() -> Result<Self, String> {
        let (target, owner) = UnixStream::pair()
            .map_err(|error| format!("failed to create the sandbox startup gate: {error}"))?;
        Ok(Self {
            target: Some(target),
            owner,
        })
    }

    pub(super) fn inherited_descriptor(&self) -> RawFd {
        self.target
            .as_ref()
            .expect("unspawned startup gate should retain its target endpoint")
            .as_raw_fd()
    }

    pub(super) fn child_spawned(&mut self) {
        drop(self.target.take());
    }

    pub(super) fn release(mut self) -> io::Result<()> {
        debug_assert!(self.target.is_none());
        self.owner.write_all(&[TARGET_GATE_RELEASE])
    }
}

impl SandboxedCommand {
    pub(crate) fn new(program: &OsStr) -> Result<Self, String> {
        let startup_gate = StartupGate::new()?;
        let target_gate_descriptor = startup_gate.inherited_descriptor();
        let executable = std::env::current_exe()
            .map_err(|error| format!("failed to locate the sandbox target gate: {error}"))?;
        let (mut command, temporary_directory) = platform::sandboxed_command()?;
        command
            .arg(executable)
            .arg("sandbox-target")
            .arg("--gate-fd")
            .arg(target_gate_descriptor.to_string())
            .arg("--")
            .arg(program);
        Ok(Self {
            command,
            temporary_directory,
            startup_gate,
        })
    }

    pub(crate) fn arg(&mut self, argument: impl AsRef<OsStr>) -> &mut Self {
        self.command.arg(argument);
        self
    }

    pub(crate) fn args<I, S>(&mut self, arguments: I) -> &mut Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<OsStr>,
    {
        for argument in arguments {
            self.arg(argument);
        }
        self
    }

    /// Adds an environment variable inherited by the sandboxed program.
    ///
    /// Host loader injection is removed from the initial `sandbox-exec`
    /// command. Callers must not restore it inside the sandbox.
    /// `TMPDIR` is reserved and reset to the private directory when spawning.
    pub(crate) fn env(&mut self, key: impl AsRef<OsStr>, value: impl AsRef<OsStr>) -> &mut Self {
        self.command.env(key, value);
        self
    }

    pub(crate) fn env_remove(&mut self, key: impl AsRef<OsStr>) -> &mut Self {
        self.command.env_remove(key);
        self
    }

    pub(crate) fn stdin(&mut self, configuration: Stdio) -> &mut Self {
        self.command.stdin(configuration);
        self
    }

    pub(crate) fn stdout(&mut self, configuration: Stdio) -> &mut Self {
        self.command.stdout(configuration);
        self
    }

    pub(crate) fn stderr(&mut self, configuration: Stdio) -> &mut Self {
        self.command.stderr(configuration);
        self
    }
}
