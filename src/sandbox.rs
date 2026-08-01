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
use wait_timeout::ChildExt as _;

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
/// not terminate the child and removes the private directory. Background
/// descendants are unsupported and may outlive this owner. Piped streams can
/// be taken and moved to independent I/O tasks before waiting.
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

    /// Isolates a background sandbox command for bounded forced termination.
    pub(crate) fn new_process_group(&mut self) -> &mut Self {
        self.command.process_group(0);
        self
    }

    /// Spawns the sandboxed program and transfers the temporary-directory
    /// guard to the returned child.
    pub(crate) fn spawn(mut self) -> Result<SandboxedChild, String> {
        self.command.env("TMPDIR", self.temporary_directory.path());
        let child = self
            .command
            .spawn()
            .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;
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

    pub(crate) fn try_wait(&mut self) -> std::io::Result<Option<ExitStatus>> {
        self.child.try_wait()
    }

    pub(crate) fn wait(mut self) -> Result<ExitStatus, String> {
        self.child
            .wait()
            .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))
    }

    /// Waits at most `timeout` for the direct sandbox process to exit.
    pub(crate) fn wait_timeout(&mut self, timeout: Duration) -> Result<Option<ExitStatus>, String> {
        self.child.wait_timeout(timeout).map_err(|error| {
            format!(
                "failed to wait for `{}` to exit: {error}",
                platform::SANDBOX_EXEC
            )
        })
    }

    /// Kills the live sandbox process group and reaps its direct process.
    ///
    /// Full descendant supervision, including a group whose leader has already
    /// exited, belongs to the sandbox lifetime supervisor.
    pub(crate) fn force_stop(&mut self) -> Result<(), String> {
        match self.child.try_wait() {
            Ok(Some(_)) => return Ok(()),
            Ok(None) => {}
            Err(error) => {
                return Err(format!(
                    "failed to read `{}` status before stopping it: {error}",
                    platform::SANDBOX_EXEC
                ));
            }
        }

        // SAFETY: `new_process_group` made the child's PID its process-group ID.
        let result = unsafe { libc::killpg(self.child.id() as libc::pid_t, libc::SIGKILL) };
        if result < 0 {
            let kill_error = std::io::Error::last_os_error();
            return match self.child.try_wait() {
                Ok(Some(_)) => Ok(()),
                Ok(None) => Err(format!(
                    "failed to stop `{}`: {kill_error}",
                    platform::SANDBOX_EXEC
                )),
                Err(wait_error) => Err(format!(
                    "failed to stop `{}`: {kill_error}; additionally failed to read its status: {wait_error}",
                    platform::SANDBOX_EXEC
                )),
            };
        }

        self.child.wait().map(|_| ()).map_err(|error| {
            format!(
                "failed to reap stopped `{}`: {error}",
                platform::SANDBOX_EXEC
            )
        })
    }
}
