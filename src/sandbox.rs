use std::ffi::{OsStr, OsString};
use std::process::{Command, ExitCode};

#[cfg(target_os = "macos")]
#[path = "sandbox/macos.rs"]
mod platform;

#[cfg(not(target_os = "macos"))]
#[path = "sandbox/unsupported.rs"]
mod platform;

pub fn run(command_line: &[OsString]) -> Result<ExitCode, String> {
    let (program, arguments) = command_line
        .split_first()
        .expect("sandbox command must include a program");
    let mut sandboxed = SandboxedCommand::new(program)?;
    platform::status(sandboxed.launcher_mut().args(arguments))
}

pub(crate) struct SandboxedCommand {
    launcher: Command,
    sandboxed_program: OsString,
    environment: Vec<(OsString, OsString)>,
    finalized: bool,
    // Keep the writable directory alive until the command owner is dropped.
    _temporary_directory: platform::TemporaryDirectory,
}

impl SandboxedCommand {
    pub(crate) fn new(program: &OsStr) -> Result<Self, String> {
        let (launcher, temporary_directory, temporary_directory_path) =
            platform::sandboxed_command()?;
        let mut sandboxed = Self {
            launcher,
            sandboxed_program: program.to_os_string(),
            environment: Vec::new(),
            finalized: false,
            _temporary_directory: temporary_directory,
        };
        sandboxed.env("TMPDIR", temporary_directory_path);
        Ok(sandboxed)
    }

    pub(crate) fn env<K, V>(&mut self, key: K, value: V) -> &mut Self
    where
        K: AsRef<OsStr>,
        V: AsRef<OsStr>,
    {
        assert!(
            !self.finalized,
            "sandbox environment must be configured before command arguments"
        );
        self.environment
            .push((key.as_ref().to_os_string(), value.as_ref().to_os_string()));
        self
    }

    pub(crate) fn launcher_mut(&mut self) -> &mut Command {
        if !self.finalized {
            for (key, value) in &self.environment {
                self.launcher.arg(environment_assignment(key, value));
            }
            self.launcher.arg(&self.sandboxed_program);
            self.finalized = true;
        }
        &mut self.launcher
    }
}

fn environment_assignment(key: &OsStr, value: &OsStr) -> OsString {
    let mut assignment = key.to_os_string();
    assignment.push("=");
    assignment.push(value);
    assignment
}
