use std::ffi::OsString;
use std::process::ExitCode;

#[cfg(target_os = "macos")]
use std::ffi::OsStr;
#[cfg(target_os = "macos")]
use std::fs::File;
#[cfg(target_os = "macos")]
use std::io::Read as _;
#[cfg(target_os = "macos")]
use std::os::fd::{AsRawFd as _, FromRawFd as _, OwnedFd};
#[cfg(target_os = "macos")]
use std::os::unix::net::UnixStream;
#[cfg(target_os = "macos")]
use std::os::unix::process::CommandExt as _;
#[cfg(target_os = "macos")]
use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command, Stdio};
#[cfg(target_os = "macos")]
use std::time::Duration;

#[cfg(target_os = "macos")]
const CRASH_MANAGER_CLEANUP_TIMEOUT: Duration = Duration::from_secs(1);
#[cfg(target_os = "macos")]
const TARGET_GATE_RELEASE: u8 = 1;

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
    let (target_gate, launcher_gate) = UnixStream::pair()
        .map_err(|error| format!("failed to create the sandbox target startup gate: {error}"))?;
    let target_gate_descriptor = target_gate.as_raw_fd();
    let executable = std::env::current_exe()
        .map_err(|error| format!("failed to locate the sandbox target gate: {error}"))?;
    let mut sandboxed = SandboxedCommand::new(executable.as_os_str())?;
    sandboxed
        .arg("sandbox-target")
        .arg("--gate-fd")
        .arg(target_gate_descriptor.to_string())
        .arg("--")
        .args(command_line)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    sandboxed.status(target_gate, launcher_gate)
}

#[cfg(target_os = "macos")]
pub(crate) fn run_manager() -> Result<(), String> {
    supervision::run_manager()
}

#[cfg(target_os = "macos")]
pub(crate) fn run_target(
    gate_descriptor: libc::c_int,
    command_line: &[OsString],
) -> Result<ExitCode, String> {
    let (program, arguments) = command_line
        .split_first()
        .expect("sandbox target must include a program");
    if gate_descriptor <= libc::STDERR_FILENO {
        return Err("sandbox target startup gate descriptor is invalid".to_string());
    }
    loop {
        if unsafe { libc::fcntl(gate_descriptor, libc::F_GETFD) } >= 0 {
            break;
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(format!(
                "sandbox target startup gate descriptor is invalid: {error}"
            ));
        }
    }
    // SAFETY: the standalone launcher transfers this inherited descriptor to
    // the hidden target process and retains no owner for the child-side copy.
    let gate = unsafe { OwnedFd::from_raw_fd(gate_descriptor) };
    let mut gate = File::from(gate);
    let mut release = [0];
    gate.read_exact(&mut release)
        .map_err(|error| format!("failed to await sandbox target startup: {error}"))?;
    if release != [TARGET_GATE_RELEASE] {
        return Err("sandbox target received an invalid startup release".to_string());
    }
    drop(gate);

    let error = Command::new(program).args(arguments).exec();
    Err(format!(
        "failed to launch sandbox target `{}`: {error}",
        program.to_string_lossy()
    ))
}

#[cfg(not(target_os = "macos"))]
pub fn run(command_line: &[OsString]) -> Result<ExitCode, String> {
    platform::run(command_line)
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn run_manager() -> Result<(), String> {
    Err("the sandbox manager is currently supported only on macOS".to_string())
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn run_target(
    _gate_descriptor: libc::c_int,
    _command_line: &[OsString],
) -> Result<ExitCode, String> {
    Err("the sandbox target gate is currently supported only on macOS".to_string())
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
/// A direct sandboxed child that retains its observed process lifetime, a
/// committed crash manager, and its private temporary directory.
///
/// Retain this owner until its process lifetime is explicitly retired.
/// Dropping it does not terminate the child and removes the private directory.
/// Piped streams can be taken and moved to independent I/O tasks before
/// retirement.
#[must_use = "retain the sandboxed child until it is explicitly retired"]
pub(crate) struct SandboxedChild {
    child: Child,
    observed_lifetime: Option<supervision::ObservedLifetime>,
    crash_manager: Option<supervision::SandboxManager>,
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
    /// commits an independent host crash manager, and transfers the private
    /// temporary-directory guard to the returned child.
    ///
    /// Darwin cannot atomically attach a descendant observer at spawn time. A
    /// process that detaches before the post-spawn root watch or a fork event is
    /// observed remains outside this command's guarantee. Abrupt owner failure
    /// is covered only after the crash manager reports readiness. The manager's
    /// later tracker cannot recover descendants that escaped before it attached.
    pub(crate) fn spawn(mut self) -> Result<SandboxedChild, String> {
        self.command.env("TMPDIR", self.temporary_directory.path());
        let mut child = self
            .command
            .spawn()
            .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;
        let observed_lifetime = match supervision::ObservedLifetime::start(child.id()) {
            Ok(lifetime) => lifetime,
            Err(error) => {
                let error =
                    stop_after_observation_failure(&mut child, self.separate_process_group, error);
                // Observation failed before ownership was established. Preserve
                // the directory because a process that escaped observation may
                // still be using it even after process-group fallback cleanup.
                std::mem::forget(self.temporary_directory);
                return Err(error);
            }
        };
        let crash_manager = match supervision::SandboxManager::start(
            child.id(),
            self.temporary_directory.path(),
            CRASH_MANAGER_CLEANUP_TIMEOUT,
        ) {
            Ok(manager) => manager,
            Err(error) => {
                let mut child = SandboxedChild {
                    child,
                    observed_lifetime: Some(observed_lifetime),
                    crash_manager: None,
                    retirement: SandboxedChildRetirement::Active,
                    separate_process_group: self.separate_process_group,
                    temporary_directory: Some(self.temporary_directory),
                };
                let cleanup = child.force_stop().err();
                return Err(cleanup.map_or(error.clone(), |cleanup| {
                    append_retirement_error(Some(error), cleanup)
                }));
            }
        };
        Ok(SandboxedChild {
            child,
            observed_lifetime: Some(observed_lifetime),
            crash_manager: Some(crash_manager),
            retirement: SandboxedChildRetirement::Active,
            separate_process_group: self.separate_process_group,
            temporary_directory: Some(self.temporary_directory),
        })
    }

    /// Runs a standalone command with launcher-owned normal retirement and an
    /// independent crash manager for abrupt launcher loss.
    ///
    /// A hidden wrapper blocks on a private release descriptor before executing
    /// the requested program. The launcher attaches its local observer, waits
    /// for the manager to adopt the temporary directory and report readiness,
    /// then releases that same root process into the program. A descendant that
    /// later becomes orphaned before either observer sees its fork remains
    /// outside that observer's guarantee. Abrupt launcher failure before manager
    /// readiness remains outside crash cleanup.
    pub(crate) fn status(
        mut self,
        target_gate: UnixStream,
        launcher_gate: UnixStream,
    ) -> Result<ExitCode, String> {
        self.command.env("TMPDIR", self.temporary_directory.path());
        supervision::status(
            self.command,
            self.temporary_directory,
            target_gate,
            launcher_gate,
        )
    }
}

#[cfg(target_os = "macos")]
fn stop_after_observation_failure(
    child: &mut Child,
    separate_process_group: bool,
    mut error: String,
) -> String {
    if separate_process_group && let Err(group_error) = platform::kill_process_group(child.id()) {
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
    /// changes, waits for committed manager cleanup, reaps the direct sandbox
    /// process, then commits the temporary-directory disposition.
    ///
    /// Process-group cleanup always runs as a backstop for unobserved same-group
    /// forks; direct-child cleanup also runs after any retirement error. The
    /// private temporary directory is preserved on any cleanup error or manager
    /// cleanup timeout because an unobserved process may remain live.
    pub(crate) fn force_stop(&mut self) -> Result<(), String> {
        match &self.retirement {
            SandboxedChildRetirement::Retired { error } => return stored_retirement_result(error),
            SandboxedChildRetirement::AwaitingReap { error } => {
                return self.reap_after_stop(error.clone());
            }
            SandboxedChildRetirement::Failed { error } => return Err(error.clone()),
            SandboxedChildRetirement::Active => {}
        }

        if let Some(manager) = self.crash_manager.as_mut() {
            manager.begin_retirement();
        }
        let mut error = None;
        let observed_lifetime = self
            .observed_lifetime
            .take()
            .expect("active sandbox child should retain its observed lifetime");
        if let Err(observation_error) = observed_lifetime.stop() {
            error = Some(append_retirement_error(error, observation_error));
        }
        // Process-group cleanup remains an independent backstop for the narrow
        // interval between a fork and its observation. Run it even when tracked
        // retirement succeeds so a same-group process cannot survive merely
        // because its parent exited before the fork event was resolved.
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
        let manager_preparation = self
            .crash_manager
            .as_mut()
            .map(supervision::SandboxManager::prepare_finish);
        let mut direct_stop_failed = false;
        if error.is_some()
            && let Err(kill_error) = self.child.kill()
            && kill_error.raw_os_error() != Some(libc::ESRCH)
        {
            error = Some(append_retirement_error(
                error,
                format!(
                    "failed to stop direct `{}` process: {kill_error}",
                    platform::SANDBOX_EXEC
                ),
            ));
            direct_stop_failed = true;
        }

        let identity_released = if direct_stop_failed {
            false
        } else {
            match self.child.wait() {
                Ok(_) => true,
                Err(wait_error) => {
                    let identity_released = wait_error.raw_os_error() == Some(libc::ECHILD);
                    error = Some(append_retirement_error(
                        error,
                        format!(
                            "failed to reap stopped `{}`: {wait_error}",
                            platform::SANDBOX_EXEC
                        ),
                    ));
                    identity_released
                }
            }
        };
        let preserve_manager_directory = error.is_some()
            || manager_preparation.is_some_and(|preparation| {
                preparation != supervision::CleanupPreparation::Complete
            });
        if let Some(manager) = self.crash_manager.take()
            && let Err(manager_error) = manager.finish(preserve_manager_directory)
        {
            error = Some(append_retirement_error(error, manager_error));
        }
        if error.is_some() || manager_preparation == Some(supervision::CleanupPreparation::TimedOut)
        {
            self.preserve_temporary_directory();
        } else {
            self.remove_temporary_directory();
        }

        if direct_stop_failed {
            let error = error.expect("direct stop failure should retain its error");
            self.retirement = SandboxedChildRetirement::Failed {
                error: error.clone(),
            };
            return Err(error);
        }
        self.retirement = if identity_released {
            SandboxedChildRetirement::Retired {
                error: error.clone(),
            }
        } else {
            SandboxedChildRetirement::AwaitingReap {
                error: error.clone(),
            }
        };
        stored_retirement_result(&error)
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

    fn remove_temporary_directory(&mut self) {
        drop(self.temporary_directory.take());
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
