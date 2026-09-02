use std::ffi::OsStr;
use std::io::Write as _;
use std::os::fd::{AsRawFd as _, RawFd};
use std::os::unix::net::UnixStream;
use std::process::{Child, Command, Stdio};

use super::{TARGET_GATE_RELEASE, file_descriptors, platform, supervision};

/// A command configured to run under the macOS sandbox.
pub(crate) struct SandboxedCommand {
    pub(super) command: Command,
    pub(super) temporary_directory: platform::TemporaryDirectory,
    pub(super) startup_gate: Option<StartupGate>,
}

pub(super) struct StartupGate {
    target: Option<UnixStream>,
    owner: UnixStream,
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
    pub(super) child: Child,
    pub(super) manager: Option<supervision::SandboxManager>,
    pub(super) retirement: SandboxedChildRetirement,
}

pub(super) enum SandboxedChildRetirement {
    Active,
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

    fn inherited_descriptor(&self) -> RawFd {
        self.target
            .as_ref()
            .expect("unspawned startup gate should retain its target endpoint")
            .as_raw_fd()
    }

    pub(super) fn child_spawned(&mut self) {
        drop(self.target.take());
    }

    pub(super) fn release(mut self) -> Result<(), String> {
        debug_assert!(self.target.is_none());
        self.owner
            .write_all(&[TARGET_GATE_RELEASE])
            .map_err(|error| format!("failed to release sandbox target startup gate: {error}"))
    }
}

impl SandboxedCommand {
    pub(crate) fn new(program: &OsStr) -> Result<Self, String> {
        let startup_gate = StartupGate::new()?;
        let target_gate_descriptor = startup_gate.inherited_descriptor();
        let executable = std::env::current_exe()
            .map_err(|error| format!("failed to locate the sandbox target gate: {error}"))?;
        let mut sandboxed = Self::new_direct(executable.as_os_str())?;
        sandboxed.startup_gate = Some(startup_gate);
        sandboxed
            .arg("sandbox-target")
            .arg("--gate-fd")
            .arg(target_gate_descriptor.to_string())
            .arg("--")
            .arg(program);
        sandboxed.configure_descriptor_boundary()?;
        Ok(sandboxed)
    }

    pub(super) fn new_direct(program: &OsStr) -> Result<Self, String> {
        let (command, temporary_directory) = platform::sandboxed_command()?;
        let temporary_directory_path = temporary_directory.path().as_os_str().to_os_string();
        let mut sandboxed = Self {
            command,
            temporary_directory,
            startup_gate: None,
        };
        sandboxed
            .env("TMPDIR", temporary_directory_path)
            .arg(program);
        Ok(sandboxed)
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

    fn configure_descriptor_boundary(&mut self) -> Result<(), String> {
        // The server may be multithreaded, so scan in the forked child. Carry
        // only the private gate through the hidden wrapper; run_target closes
        // it before relay exec.
        let inherited_descriptors = self
            .startup_gate
            .as_ref()
            .map(StartupGate::inherited_descriptor)
            .into_iter()
            .collect();
        file_descriptors::close_unlisted_from_multithreaded_parent_except(
            &mut self.command,
            inherited_descriptors,
        )?;
        Ok(())
    }
}
