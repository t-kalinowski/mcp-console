use std::ffi::OsString;
use std::process::ExitCode;

#[cfg(target_os = "macos")]
use std::ffi::OsStr;
#[cfg(target_os = "macos")]
use std::fs::File;
#[cfg(target_os = "macos")]
use std::io::{Read, Write};
#[cfg(target_os = "macos")]
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
#[cfg(target_os = "macos")]
use std::os::unix::net::UnixStream;
#[cfg(target_os = "macos")]
use std::os::unix::process::CommandExt as _;
#[cfg(target_os = "macos")]
use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command, Stdio};
#[cfg(target_os = "macos")]
use std::time::Duration;

#[cfg(target_os = "macos")]
const STARTUP_CONTROL_DESCRIPTOR: &str = "MCP_CONSOLE_SANDBOX_STARTUP_FD";
#[cfg(target_os = "macos")]
const STARTUP_READY: u8 = 1;
#[cfg(target_os = "macos")]
const STARTUP_GO: u8 = 2;
#[cfg(target_os = "macos")]
const STARTUP_TIMEOUT: Duration = Duration::from_secs(5);
#[cfg(target_os = "macos")]
const BACKGROUND_CLEANUP_TIMEOUT: Duration = Duration::from_secs(1);
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

#[cfg(target_os = "macos")]
pub(crate) fn await_sandbox_startup() -> Result<(), String> {
    let descriptor = match std::env::var(STARTUP_CONTROL_DESCRIPTOR) {
        Ok(descriptor) => descriptor
            .parse::<libc::c_int>()
            .map_err(|_| "sandbox startup control descriptor is invalid".to_string())?,
        Err(std::env::VarError::NotPresent) => return Ok(()),
        Err(std::env::VarError::NotUnicode(_)) => {
            return Err("sandbox startup control descriptor is invalid".to_string());
        }
    };
    if descriptor <= libc::STDERR_FILENO {
        return Err("sandbox startup control descriptor is invalid".to_string());
    }
    // SAFETY: the built-in relay reaches this gate before it starts any
    // threads, so no other thread can inspect or mutate the process environment.
    unsafe {
        std::env::remove_var(STARTUP_CONTROL_DESCRIPTOR);
    }
    // SAFETY: the trusted host supplied this owned descriptor so it can commit
    // sandbox tracking before the relay launches the worker.
    let mut control = unsafe { UnixStream::from_raw_fd(descriptor) };
    control
        .set_read_timeout(Some(STARTUP_TIMEOUT))
        .and_then(|()| control.set_write_timeout(Some(STARTUP_TIMEOUT)))
        .map_err(|error| format!("failed to configure sandbox startup gate: {error}"))?;
    control
        .write_all(&[STARTUP_READY])
        .map_err(|error| format!("failed to report sandbox startup readiness: {error}"))?;
    let mut command = [0];
    control
        .read_exact(&mut command)
        .map_err(|error| format!("sandbox startup was not committed: {error}"))?;
    if command != [STARTUP_GO] {
        return Err("sandbox startup commit is invalid".to_string());
    }
    Ok(())
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
/// assert!(child
///     .wait_timeout_without_reaping(Duration::from_secs(1))
///     .expect("child exit should be observable"));
/// child.force_stop().expect("child should be retired");
/// ```
pub(crate) struct SandboxedCommand {
    command: Command,
    temporary_directory: platform::TemporaryDirectory,
    startup_gate: Option<StartupGate>,
}

#[cfg(target_os = "macos")]
struct StartupGate {
    parent: UnixStream,
    child: UnixStream,
}

#[cfg(target_os = "macos")]
/// A direct sandboxed child and its host-owned lifetime state.
///
/// Retain this owner until retirement, then call `force_stop` to retire the
/// sandbox root and observed descendants and reap its direct process. Dropping
/// this owner runs the same path on a best-effort basis, keeping sandbox-lifetime
/// cleanup before direct-process reaping. Piped streams can be taken and moved
/// to independent I/O tasks before retirement.
#[must_use = "retain the sandboxed child until it is explicitly retired"]
pub(crate) struct SandboxedChild {
    child: Child,
    manager: Option<supervision::SandboxManager>,
    retirement: SandboxedChildRetirement,
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

    /// Holds the built-in relay at process entry until host-side sandbox
    /// tracking and temporary-directory ownership are committed.
    pub(crate) fn gate_startup(&mut self) -> Result<&mut Self, String> {
        assert!(
            self.startup_gate.is_none(),
            "sandbox startup can be gated only once"
        );
        let (parent, child) = UnixStream::pair()
            .map_err(|error| format!("failed to create sandbox startup control: {error}"))?;
        self.command
            .env(STARTUP_CONTROL_DESCRIPTOR, child.as_raw_fd().to_string());
        self.startup_gate = Some(StartupGate { parent, child });
        Ok(self)
    }

    /// Spawns the sandboxed program under a host-side sandbox lifetime manager.
    pub(crate) fn spawn(mut self) -> Result<SandboxedChild, String> {
        self.command
            .env("TMPDIR", self.temporary_directory.path())
            .process_group(0);
        let inherited_descriptors = self
            .startup_gate
            .as_ref()
            .map(|gate| vec![gate.child.as_raw_fd()])
            .unwrap_or_default();
        supervision::configure_command(&mut self.command, inherited_descriptors)?;
        let mut manager = supervision::SandboxManager::spawn(BACKGROUND_CLEANUP_TIMEOUT)?;
        let mut child = self
            .command
            .spawn()
            .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;
        let gated_startup = self.startup_gate.is_some();

        let startup_control = match self.startup_gate.take() {
            Some(gate) => {
                drop(gate.child);
                match wait_for_startup_ready(gate.parent) {
                    Ok(control) => Some(control),
                    Err(error) => {
                        drop(manager);
                        return Err(stop_unmanaged_child(&mut child, error));
                    }
                }
            }
            None => None,
        };

        if let Err(error) = manager.observe(child.id(), self.temporary_directory.path()) {
            if !gated_startup {
                self.temporary_directory.preserve();
            }
            drop(manager);
            return Err(stop_unmanaged_child(&mut child, error));
        }
        manager.monitor(child.id(), self.temporary_directory);
        if let Err(error) = manager.commit() {
            let manager_error = manager.stop().err();
            return Err(stop_after_manager_failure(&mut child, error, manager_error));
        }
        if let Some(mut control) = startup_control
            && let Err(error) = control.write_all(&[STARTUP_GO])
        {
            let error = format!("failed to commit sandbox startup: {error}");
            let manager_error = manager.stop().err();
            return Err(stop_after_manager_failure(&mut child, error, manager_error));
        }

        Ok(SandboxedChild {
            child,
            manager: Some(manager),
            retirement: SandboxedChildRetirement::Active,
        })
    }

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
fn wait_for_startup_ready(mut control: UnixStream) -> Result<UnixStream, String> {
    control
        .set_read_timeout(Some(STARTUP_TIMEOUT))
        .and_then(|()| control.set_write_timeout(Some(STARTUP_TIMEOUT)))
        .map_err(|error| format!("failed to configure sandbox startup control: {error}"))?;
    let mut ready = [0];
    control
        .read_exact(&mut ready)
        .map_err(|error| format!("sandboxed child did not reach its startup gate: {error}"))?;
    if ready != [STARTUP_READY] {
        return Err("sandboxed child sent an invalid startup response".to_string());
    }
    Ok(control)
}

#[cfg(target_os = "macos")]
fn stop_after_manager_failure(
    child: &mut Child,
    mut error: String,
    manager_error: Option<String>,
) -> String {
    let Some(manager_error) = manager_error else {
        if let Err(wait_error) = child.wait() {
            error.push_str(&format!(
                "; additionally failed to reap `{}`: {wait_error}",
                platform::SANDBOX_EXEC
            ));
        }
        return error;
    };
    error.push_str(&format!("; additionally, {manager_error}"));
    stop_unmanaged_child(child, error)
}

#[cfg(target_os = "macos")]
fn stop_unmanaged_child(child: &mut Child, mut error: String) -> String {
    if let Err(group_error) = platform::kill_process_group(child.id()) {
        error.push_str(&format!(
            "; additionally failed to stop `{}` process group: {group_error}",
            platform::SANDBOX_EXEC
        ));
    }
    let exited =
        platform::wait_for_process_exit_without_reaping(child.id(), Duration::from_secs(1))
            .unwrap_or(false);
    if !exited
        && let Err(kill_error) = child.kill()
        && kill_error.raw_os_error() != Some(libc::ESRCH)
    {
        error.push_str(&format!(
            "; additionally failed to stop `{}`: {kill_error}",
            platform::SANDBOX_EXEC
        ));
    }
    if let Err(wait_error) = child.wait() {
        error.push_str(&format!(
            "; additionally failed to reap `{}`: {wait_error}",
            platform::SANDBOX_EXEC
        ));
    }
    error
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
    /// Retaining the waitable child pins its PID, which is also its process-group
    /// ID, until sandbox-lifetime cleanup completes and the direct process is
    /// reaped.
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

    /// Stops the root and observed descendants through the host-side manager or
    /// its recovery monitor, then reaps the direct sandbox process.
    pub(crate) fn force_stop(&mut self) -> Result<(), String> {
        match &self.retirement {
            SandboxedChildRetirement::Retired { error } => return stored_retirement_result(error),
            SandboxedChildRetirement::AwaitingReap { error } => {
                return self.reap_after_stop(error.clone());
            }
            SandboxedChildRetirement::Failed { error } => return Err(error.clone()),
            SandboxedChildRetirement::Active => {}
        }

        let manager = self
            .manager
            .take()
            .expect("active sandbox child should retain its lifetime manager");
        let mut error = manager.stop().err();
        if error.is_some() {
            if let Err(group_error) = platform::kill_process_group(self.child.id()) {
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
impl Drop for SandboxedChild {
    fn drop(&mut self) {
        let _ = self.force_stop();
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
