use std::ffi::{OsStr, OsString};
use std::process::{Command, ExitCode};

#[cfg(target_os = "macos")]
#[path = "sandbox/macos.rs"]
mod platform;

#[cfg(not(target_os = "macos"))]
#[path = "sandbox/unsupported.rs"]
mod platform;

pub fn run(command: &[OsString]) -> Result<ExitCode, String> {
    platform::run(command)
}

pub(crate) struct WorkerCommand {
    command: Command,
    program: OsString,
    environment: Vec<(OsString, OsString)>,
    finalized: bool,
    _temporary_directory: platform::TemporaryDirectory,
}

impl WorkerCommand {
    pub(crate) fn env<K, V>(&mut self, key: K, value: V) -> &mut Self
    where
        K: AsRef<OsStr>,
        V: AsRef<OsStr>,
    {
        assert!(
            !self.finalized,
            "worker environment must be configured before command arguments"
        );
        self.environment
            .push((key.as_ref().to_os_string(), value.as_ref().to_os_string()));
        self
    }

    pub(crate) fn command_mut(&mut self) -> &mut Command {
        if !self.finalized {
            for (key, value) in &self.environment {
                let mut assignment = key.clone();
                assignment.push("=");
                assignment.push(value);
                self.command.arg(assignment);
            }
            self.command.arg(&self.program);
            self.finalized = true;
        }
        &mut self.command
    }
}

pub(crate) fn worker_command(program: &OsStr) -> Result<WorkerCommand, String> {
    let (command, temporary_directory) = platform::worker_command()?;
    Ok(WorkerCommand {
        command,
        program: program.to_os_string(),
        environment: Vec::new(),
        finalized: false,
        _temporary_directory: temporary_directory,
    })
}
