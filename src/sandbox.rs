use std::ffi::OsString;
use std::process::ExitCode;

#[cfg(target_os = "macos")]
use std::ffi::OsStr;
#[cfg(target_os = "macos")]
use std::process::Command;

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
    sandboxed.command_mut().args(arguments);
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
impl SandboxedCommand {
    pub(crate) fn new(program: &OsStr) -> Result<Self, String> {
        let (mut command, temporary_directory) = platform::sandboxed_command()?;
        command
            .env("TMPDIR", temporary_directory.path())
            .arg(program);
        Ok(Self {
            command,
            temporary_directory,
        })
    }

    /// Returns the `sandbox-exec` command for pre-launch configuration.
    ///
    /// Arguments added to it follow the sandboxed program.
    /// Environment and stdio settings are inherited by the sandboxed program.
    /// macOS filters `DYLD_*` variables when launching `sandbox-exec`; this
    /// wrapper intentionally does not restore them inside the sandbox.
    ///
    /// Launch only through `SandboxedCommand` so the private temporary
    /// directory remains available to the child. `TMPDIR` is reserved and
    /// reset to that directory when the command launches.
    pub(crate) fn command_mut(&mut self) -> &mut Command {
        &mut self.command
    }

    pub(crate) fn status(mut self) -> Result<ExitCode, String> {
        self.command.env("TMPDIR", self.temporary_directory.path());
        platform::status(&mut self.command)
    }
}
