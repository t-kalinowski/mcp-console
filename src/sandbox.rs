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
    let mut sandboxed = SandboxedCommandBuilder::new(program)?.finalize();
    platform::status(sandboxed.launcher_mut().args(arguments))
}

#[cfg(not(target_os = "macos"))]
pub fn run(command_line: &[OsString]) -> Result<ExitCode, String> {
    platform::run(command_line)
}

#[cfg(target_os = "macos")]
pub(crate) struct SandboxedCommandBuilder {
    launcher: Command,
    sandboxed_program: OsString,
    temporary_directory: platform::TemporaryDirectory,
}

#[cfg(target_os = "macos")]
pub(crate) struct SandboxedCommand {
    launcher: Command,
    // Keep the writable directory alive until the command owner is dropped.
    _temporary_directory: platform::TemporaryDirectory,
}

#[cfg(target_os = "macos")]
impl SandboxedCommandBuilder {
    pub(crate) fn new(program: &OsStr) -> Result<Self, String> {
        let (launcher, temporary_directory, temporary_directory_path) =
            platform::sandboxed_command()?;
        let mut builder = Self {
            launcher,
            sandboxed_program: program.to_os_string(),
            temporary_directory,
        };
        builder.env("TMPDIR", temporary_directory_path);
        Ok(builder)
    }

    /// Adds a variable to the environment inherited through `sandbox-exec`
    /// without encoding it in the launcher arguments.
    ///
    /// macOS filters `DYLD_*` variables when launching `sandbox-exec`; this
    /// builder intentionally does not restore them inside the sandbox.
    pub(crate) fn env<K, V>(&mut self, key: K, value: V) -> &mut Self
    where
        K: AsRef<OsStr>,
        V: AsRef<OsStr>,
    {
        self.launcher.env(key, value);
        self
    }

    pub(crate) fn finalize(mut self) -> SandboxedCommand {
        self.launcher.arg(&self.sandboxed_program);

        SandboxedCommand {
            launcher: self.launcher,
            _temporary_directory: self.temporary_directory,
        }
    }
}

#[cfg(target_os = "macos")]
impl SandboxedCommand {
    pub(crate) fn launcher_mut(&mut self) -> &mut Command {
        &mut self.launcher
    }
}
