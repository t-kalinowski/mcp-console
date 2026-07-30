use std::ffi::OsString;
use std::process::ExitCode;

#[cfg(target_os = "macos")]
use std::ffi::OsStr;
#[cfg(target_os = "macos")]
use std::process::{Child, Command, ExitStatus, Stdio};

#[cfg(target_os = "macos")]
#[path = "sandbox/macos.rs"]
mod platform;

#[cfg(not(target_os = "macos"))]
#[path = "sandbox/unsupported.rs"]
mod platform;

#[cfg(target_os = "macos")]
pub fn run(command_line: &[OsString]) -> Result<ExitCode, String> {
    let (program, arguments) = command_line
        .split_first()
        .expect("sandbox command must include a program");
    let mut sandboxed = SandboxedCommand::new(program)?;
    sandboxed
        .args(arguments)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    sandboxed.status()
}

#[cfg(not(target_os = "macos"))]
pub fn run(command_line: &[OsString]) -> Result<ExitCode, String> {
    platform::run(command_line)
}

#[cfg(target_os = "macos")]
pub(crate) struct SandboxedCommand {
    command: Command,
    temporary_directory: platform::TemporaryDirectory,
}

#[cfg(target_os = "macos")]
/// A direct sandboxed child that retains its private temporary directory.
///
/// Retain this owner until the child exits, then call `wait`. Dropping it does
/// not terminate the child and removes the private directory.
#[must_use = "retain the sandboxed child until it is explicitly waited"]
pub(crate) struct SandboxedChild {
    child: Child,
    _temporary_directory: platform::TemporaryDirectory,
}

#[cfg(target_os = "macos")]
impl SandboxedCommand {
    pub(crate) fn new(program: &OsStr) -> Result<Self, String> {
        let (command, temporary_directory) = platform::sandboxed_command()?;
        let temporary_directory_path = temporary_directory.path().as_os_str().to_os_string();
        let mut sandboxed = Self {
            command,
            temporary_directory,
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
    /// macOS filters `DYLD_*` variables when launching `sandbox-exec`; this
    /// wrapper intentionally does not restore them inside the sandbox.
    /// `TMPDIR` is reserved and reset to the private directory when spawning.
    pub(crate) fn env(&mut self, key: impl AsRef<OsStr>, value: impl AsRef<OsStr>) -> &mut Self {
        self.command.env(key, value);
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

    /// Spawns the sandboxed program and transfers the temporary-directory
    /// guard to the returned child.
    pub(crate) fn spawn(mut self) -> Result<SandboxedChild, String> {
        self.command.env("TMPDIR", self.temporary_directory.path());
        let child = platform::spawn(&mut self.command)?;
        Ok(SandboxedChild {
            child,
            _temporary_directory: self.temporary_directory,
        })
    }

    pub(crate) fn status(self) -> Result<ExitCode, String> {
        let status = self.spawn()?.wait()?;
        Ok(platform::exit_code(status))
    }
}

#[cfg(target_os = "macos")]
impl SandboxedChild {
    pub(crate) fn wait(mut self) -> Result<ExitStatus, String> {
        platform::wait(&mut self.child)
    }
}
