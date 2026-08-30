use std::ffi::OsString;
use std::process::ExitCode;

#[cfg(target_os = "macos")]
use std::ffi::OsStr;
#[cfg(target_os = "macos")]
use std::os::unix::process::CommandExt as _;
#[cfg(target_os = "macos")]
use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command, Stdio};
#[cfg(target_os = "macos")]
use std::time::Duration;

#[cfg(target_os = "macos")]
#[path = "sandbox/file_descriptors.rs"]
mod file_descriptors;

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
/// use std::time::Duration;
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
///     .new_process_group()
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
/// assert!(child
///     .wait_timeout_without_reaping(Duration::from_secs(1))
///     .expect("child exit should be observable"));
/// child.force_stop().expect("child should be retired");
/// ```
pub(crate) struct SandboxedCommand {
    command: Command,
    temporary_directory: platform::TemporaryDirectory,
    separate_process_group: bool,
}

#[cfg(target_os = "macos")]
/// A direct sandboxed child that retains its observed process lifetime and
/// private temporary directory.
///
/// Retain this owner until its process lifetime is explicitly retired.
/// Dropping it does not terminate the child and removes the private directory.
/// Piped streams can be taken and moved to independent I/O tasks before
/// retirement.
#[must_use = "retain the sandboxed child until it is explicitly retired"]
pub(crate) struct SandboxedChild {
    child: Child,
    observed_lifetime: Option<supervision::ObservedLifetime>,
    retirement: SandboxedChildRetirement,
    separate_process_group: bool,
    temporary_directory: Option<platform::TemporaryDirectory>,
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
            separate_process_group: false,
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
        self.separate_process_group = true;
        self
    }

    /// Prevents descriptors other than fd 0, 1, and 2 from crossing exec.
    ///
    /// The caller may be multithreaded, so the descriptor scan runs in the
    /// forked child instead of relying on a parent-side snapshot.
    pub(crate) fn inherit_only_standard_streams(&mut self) -> Result<&mut Self, String> {
        file_descriptors::close_unlisted_from_multithreaded_parent(&mut self.command)?;
        Ok(self)
    }

    /// Spawns the sandboxed program, starts host-side descendant observation,
    /// and transfers the temporary-directory guard to the returned child.
    ///
    /// Darwin cannot atomically attach a descendant observer at spawn time. A
    /// process that detaches before the post-spawn root watch or a fork event is
    /// observed remains outside this command's guarantee. Failure of the owner
    /// process itself is also outside this lifetime; crash-independent ownership
    /// is a separate supervision layer.
    pub(crate) fn spawn(mut self) -> Result<SandboxedChild, String> {
        self.command.env("TMPDIR", self.temporary_directory.path());
        let mut child = self
            .command
            .spawn()
            .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;
        let observed_lifetime = match supervision::ObservedLifetime::start(child.id()) {
            Ok(lifetime) => lifetime,
            Err(error) => {
                let error = stop_after_observation_failure(
                    &mut child,
                    self.separate_process_group,
                    error,
                );
                // Observation failed before ownership was established. Preserve
                // the directory because a process that escaped observation may
                // still be using it even after process-group fallback cleanup.
                std::mem::forget(self.temporary_directory);
                return Err(error);
            }
        };
        Ok(SandboxedChild {
            child,
            observed_lifetime: Some(observed_lifetime),
            retirement: SandboxedChildRetirement::Active,
            separate_process_group: self.separate_process_group,
            temporary_directory: Some(self.temporary_directory),
        })
    }

    /// Runs a standalone command and retires descendants observed from its root.
    ///
    /// Darwin cannot atomically attach a descendant observer at spawn time. A
    /// process that detaches before the post-spawn root watch or a fork event is
    /// observed remains outside this command's guarantee. Termination or failure
    /// of the launcher itself is intentionally outside this command's scope.
    pub(crate) fn status(mut self) -> Result<ExitCode, String> {
        self.command.env("TMPDIR", self.temporary_directory.path());
        supervision::status(self.command, self.temporary_directory)
    }
}

#[cfg(target_os = "macos")]
fn stop_after_observation_failure(
    child: &mut Child,
    separate_process_group: bool,
    mut error: String,
) -> String {
    if separate_process_group
        && let Err(group_error) = platform::kill_process_group(child.id())
    {
        error = append_retirement_error(
            Some(error),
            format!(
                "failed to stop `{}` process group: {group_error}",
                platform::SANDBOX_EXEC
            ),
        );
    }
    supervision::stop_direct_child(child, error)
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

    /// Waits at most `timeout` for the direct sandbox process to exit without
    /// reaping it.
    ///
    /// Retaining the waitable child pins its PID while observed-descendant
    /// retirement runs. Callers that requested `new_process_group` also retain
    /// the group identity for fallback cleanup until `force_stop` reaps the root.
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

    /// Stops the root and descendants observed across process-group and session
    /// changes, then reaps the direct sandbox process.
    ///
    /// If observed-tree retirement fails, process-group and direct-child cleanup
    /// still run as a fallback. The private temporary directory is preserved on
    /// any cleanup error because an unobserved process may remain live.
    pub(crate) fn force_stop(&mut self) -> Result<(), String> {
        match &self.retirement {
            SandboxedChildRetirement::Retired { error } => return stored_retirement_result(error),
            SandboxedChildRetirement::AwaitingReap { error } => {
                return self.reap_after_stop(error.clone());
            }
            SandboxedChildRetirement::Failed { error } => return Err(error.clone()),
            SandboxedChildRetirement::Active => {}
        }

        let observed_lifetime = self
            .observed_lifetime
            .take()
            .expect("active sandbox child should retain its observed lifetime");
        let mut error = observed_lifetime.stop().err();
        if error.is_some() {
            if self.separate_process_group
                && let Err(group_error) = platform::kill_process_group(self.child.id())
            {
                error = Some(append_retirement_error(
                    error,
                    format!(
                        "failed to stop `{}` process group: {group_error}",
                        platform::SANDBOX_EXEC
                    ),
                ));
            }
            if let Err(kill_error) = self.child.kill()
                && kill_error.raw_os_error() != Some(libc::ESRCH)
            {
                let error = append_retirement_error(
                    error,
                    format!(
                        "failed to stop direct `{}` process: {kill_error}",
                        platform::SANDBOX_EXEC
                    ),
                );
                self.preserve_temporary_directory();
                self.retirement = SandboxedChildRetirement::Failed {
                    error: error.clone(),
                };
                return Err(error);
            }
        }

        self.retirement = SandboxedChildRetirement::AwaitingReap {
            error: error.clone(),
        };
        self.reap_after_stop(error)
    }

    fn reap_after_stop(&mut self, prior_error: Option<String>) -> Result<(), String> {
        match self.child.wait() {
            Ok(_) => {
                if prior_error.is_some() {
                    self.preserve_temporary_directory();
                }
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
                self.preserve_temporary_directory();
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

    fn preserve_temporary_directory(&mut self) {
        if let Some(directory) = self.temporary_directory.take() {
            std::mem::forget(directory);
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
