use std::ffi::OsString;
use std::process::ExitCode;

#[cfg(target_os = "macos")]
use std::ffi::OsStr;
#[cfg(target_os = "macos")]
use std::os::unix::process::CommandExt as _;
#[cfg(target_os = "macos")]
use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command, ExitStatus, Stdio};
#[cfg(target_os = "macos")]
use std::time::Duration;

#[cfg(target_os = "macos")]
#[path = "sandbox/macos.rs"]
mod platform;

#[cfg(target_os = "macos")]
#[path = "sandbox/supervision.rs"]
mod supervision;

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
/// A command configured to run under the macOS sandbox.
///
/// The public sandbox transcript exercises this interaction. This example is
/// ignored as a doctest because the type is crate-private in a binary target.
///
/// # Example
///
/// ```ignore
/// use crate::sandbox::SandboxedCommand;
/// use std::ffi::OsStr;
/// use std::io::{Read, Write};
/// use std::process::Stdio;
///
/// fn read_echo(mut stream: impl Read) -> [u8; 6] {
///     let mut output = [0; 6];
///     stream
///         .read_exact(&mut output)
///         .expect("output should be readable");
///     output
/// }
///
/// let script = r#"
/// import sys
///
/// for line in sys.stdin:
///     if line == "EXIT\n":
///         break
///
///     sys.stdout.write(line)
///     sys.stdout.flush()
///     sys.stderr.write(line)
///     sys.stderr.flush()
/// "#;
///
/// let mut command =
///     SandboxedCommand::new(OsStr::new("python")).expect("sandbox should be configured");
/// command
///     .args(["-c", script])
///     .stdin(Stdio::piped())
///     .stdout(Stdio::piped())
///     .stderr(Stdio::piped());
///
/// let mut child = command.spawn().expect("sandboxed Python should spawn");
/// let mut stdin = child.take_stdin().expect("stdin should be piped");
/// let stdout = child.take_stdout().expect("stdout should be piped");
/// let stderr = child.take_stderr().expect("stderr should be piped");
/// let stdout = std::thread::spawn(move || read_echo(stdout));
/// let stderr = std::thread::spawn(move || read_echo(stderr));
///
/// stdin
///     .write_all(b"hello\n")
///     .expect("input should be written");
/// assert_eq!(stdout.join().expect("stdout reader should finish"), *b"hello\n");
/// assert_eq!(stderr.join().expect("stderr reader should finish"), *b"hello\n");
///
/// stdin
///     .write_all(b"EXIT\n")
///     .expect("EXIT should be written");
/// assert!(child.wait().expect("child should exit").success());
/// ```
pub(crate) struct SandboxedCommand {
    command: Command,
    temporary_directory: platform::TemporaryDirectory,
}

#[cfg(target_os = "macos")]
/// A direct sandboxed child that retains its private temporary directory.
///
/// Retain this owner until the child exits, then call `wait`. Dropping it does
/// not terminate the child and removes the private directory. `wait` does not
/// stop background descendants; use `force_stop` when retiring a process-group
/// lifetime. Piped streams can be taken and moved to independent I/O tasks
/// before waiting.
#[must_use = "retain the sandboxed child until it is explicitly waited"]
pub(crate) struct SandboxedChild {
    child: Child,
    retirement: SandboxedChildRetirement,
    _temporary_directory: platform::TemporaryDirectory,
}

#[cfg(target_os = "macos")]
enum SandboxedChildRetirement {
    Active,
    AwaitingReap { error: Option<String> },
    Retired { error: Option<String> },
    Failed { error: String },
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

    /// Isolates a background sandbox command for bounded forced termination.
    pub(crate) fn new_process_group(&mut self) -> &mut Self {
        self.command.process_group(0);
        self
    }

    /// Spawns the sandboxed program and transfers the temporary-directory
    /// guard to the returned child.
    pub(crate) fn spawn(mut self) -> Result<SandboxedChild, String> {
        self.command.env("TMPDIR", self.temporary_directory.path());
        supervision::configure_command(&mut self.command)?;
        let child = self
            .command
            .spawn()
            .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;
        Ok(SandboxedChild {
            child,
            retirement: SandboxedChildRetirement::Active,
            _temporary_directory: self.temporary_directory,
        })
    }

    pub(crate) fn status(mut self) -> Result<ExitCode, String> {
        self.command.env("TMPDIR", self.temporary_directory.path());
        supervision::status(self.command, self.temporary_directory)
    }
}

#[cfg(target_os = "macos")]
impl SandboxedChild {
    #[allow(dead_code, reason = "used by spawned callers with piped stdin")]
    pub(crate) fn take_stdin(&mut self) -> Option<ChildStdin> {
        self.child.stdin.take()
    }

    #[allow(dead_code, reason = "used by spawned callers with piped stdout")]
    pub(crate) fn take_stdout(&mut self) -> Option<ChildStdout> {
        self.child.stdout.take()
    }

    #[allow(dead_code, reason = "used by spawned callers with piped stderr")]
    pub(crate) fn take_stderr(&mut self) -> Option<ChildStderr> {
        self.child.stderr.take()
    }

    #[allow(dead_code, reason = "retained for spawned callers awaiting normal exit")]
    pub(crate) fn wait(mut self) -> Result<ExitStatus, String> {
        self.child
            .wait()
            .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))
    }

    /// Waits at most `timeout` for the direct sandbox process to exit without
    /// reaping it.
    ///
    /// Retaining the waitable child pins its PID, which is also the process
    /// group ID created by `new_process_group`, until `force_stop` completes
    /// exact group cleanup and reaps the direct process.
    pub(crate) fn wait_timeout_without_reaping(&self, timeout: Duration) -> Result<bool, String> {
        match &self.retirement {
            SandboxedChildRetirement::Retired { .. } => return Ok(true),
            SandboxedChildRetirement::Failed { error } => return Err(error.clone()),
            SandboxedChildRetirement::Active | SandboxedChildRetirement::AwaitingReap { .. } => {}
        }
        platform::wait_for_process_exit_without_reaping(self.child.id(), timeout).map_err(|error| {
            format!(
                "failed to wait for `{}` to exit without reaping it: {error}",
                platform::SANDBOX_EXEC
            )
        })
    }

    /// Kills the sandbox process group and reaps its direct process.
    ///
    /// Group cleanup still runs when the direct process has already exited so
    /// descendants cannot outlive the sandbox lifetime supervisor.
    pub(crate) fn force_stop(&mut self) -> Result<(), String> {
        let prior_error = match &self.retirement {
            SandboxedChildRetirement::Retired { error } => return stored_retirement_result(error),
            SandboxedChildRetirement::AwaitingReap { error } => {
                return self.reap_after_stop(error.clone());
            }
            SandboxedChildRetirement::Failed { error } => return Err(error.clone()),
            SandboxedChildRetirement::Active => None,
        };

        // `new_process_group` made the child's PID its process-group ID. If
        // descendant cleanup fails, still stop and reap the direct child while
        // preserving that error so a replacement is not started.
        if let Err(group_error) = platform::kill_process_group(self.child.id()) {
            let group_error = append_retirement_error(
                prior_error,
                format!(
                    "failed to stop `{}` process group: {group_error}",
                    platform::SANDBOX_EXEC
                ),
            );
            if let Err(kill_error) = self.child.kill()
                && kill_error.raw_os_error() != Some(libc::ESRCH)
            {
                let error = append_retirement_error(
                    Some(group_error),
                    format!(
                        "failed to stop direct `{}` process: {kill_error}",
                        platform::SANDBOX_EXEC
                    ),
                );
                self.retirement = SandboxedChildRetirement::Failed {
                    error: error.clone(),
                };
                return Err(error);
            }
            self.retirement = SandboxedChildRetirement::AwaitingReap {
                error: Some(group_error.clone()),
            };
            return self.reap_after_stop(Some(group_error));
        }

        self.retirement = SandboxedChildRetirement::AwaitingReap {
            error: prior_error.clone(),
        };
        self.reap_after_stop(prior_error)
    }

    fn reap_after_stop(&mut self, prior_error: Option<String>) -> Result<(), String> {
        match self.child.wait() {
            Ok(_) => {
                self.retirement = SandboxedChildRetirement::Retired {
                    error: prior_error.clone(),
                };
                stored_retirement_result(&prior_error)
            }
            Err(wait_error) => {
                let identity_released = wait_error.raw_os_error() == Some(libc::ECHILD);
                let error = append_retirement_error(
                    prior_error,
                    format!(
                        "failed to reap stopped `{}`: {wait_error}",
                        platform::SANDBOX_EXEC
                    ),
                );
                if identity_released {
                    self.retirement = SandboxedChildRetirement::Retired {
                        error: Some(error.clone()),
                    };
                } else {
                    self.retirement = SandboxedChildRetirement::AwaitingReap {
                        error: Some(error.clone()),
                    };
                }
                Err(error)
            }
        }
    }
}

#[cfg(target_os = "macos")]
fn append_retirement_error(prior: Option<String>, error: String) -> String {
    prior.map_or(error.clone(), |prior| {
        format!("{prior}; additionally {error}")
    })
}

#[cfg(target_os = "macos")]
fn stored_retirement_result(error: &Option<String>) -> Result<(), String> {
    error.as_ref().map_or(Ok(()), |error| Err(error.clone()))
}

#[cfg(target_os = "macos")]
/// Kills every other live member of the caller's sandbox process group.
///
/// The sandbox relay remains alive to reap its direct worker and flush its
/// protocol output. Fail fast unless the caller is the process-group leader so
/// this cannot accidentally target an inherited server process group.
pub(crate) fn force_stop_process_group_members_except_self() -> Result<(), String> {
    let process_id = std::process::id();
    // SAFETY: `getpgrp` has no error return and reads the calling process's
    // current process-group ID.
    let process_group_id = unsafe { libc::getpgrp() };
    if process_group_id != process_id as libc::pid_t {
        return Err("sandbox relay is not its process-group leader".to_string());
    }

    platform::kill_process_group_members_except(process_id, process_id)
        .map_err(|error| format!("failed to stop sandbox process-group members: {error}"))
}
