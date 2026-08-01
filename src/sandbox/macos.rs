mod file_descriptors;
mod job_control;
mod process;
mod process_tracker;

use self::file_descriptors::close_unlisted_on_exec;
use self::job_control::{ForegroundTerminal, SignalRelay};
use self::process_tracker::{DescendantTracker, EventWait};
use std::ffi::OsString;
use std::fs::{self, DirBuilder};
use std::os::fd::RawFd;
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, ExitStatus};
use std::time::{SystemTime, UNIX_EPOCH};

const SANDBOX_EXEC: &str = "/usr/bin/sandbox-exec";
const POLICY: &str = include_str!("read_only_policy.sbpl");
const INHERITED_DESCRIPTORS: [RawFd; 3] =
    [libc::STDIN_FILENO, libc::STDOUT_FILENO, libc::STDERR_FILENO];

pub(super) fn run(command: &[OsString]) -> Result<ExitCode, String> {
    let mut temp_directory = TemporaryDirectory::new()?;
    let signal_relay = SignalRelay::install()?;
    let mut foreground_terminal = ForegroundTerminal::detect();

    let mut sandbox_command = Command::new(SANDBOX_EXEC);
    sandbox_command
        .arg("-p")
        .arg(POLICY)
        .arg(parameter_definition(
            "TEMP_DIRECTORY",
            temp_directory.path(),
        ))
        .arg("--")
        .args(command)
        .env("TMPDIR", temp_directory.path());
    signal_relay.configure_child(&mut sandbox_command, foreground_terminal.descriptor());
    close_unlisted_on_exec(&INHERITED_DESCRIPTORS)?;

    let mut child = sandbox_command
        .spawn()
        .map_err(|error| format!("failed to launch `{SANDBOX_EXEC}`: {error}"))?;

    let mut tracker = match DescendantTracker::start(child.id() as libc::pid_t, &signal_relay) {
        Ok(tracker) => tracker,
        Err(error) => {
            let _ = kill_root(&mut child);
            temp_directory.preserve();
            return Err(error);
        }
    };

    let status_result = wait_for_root(&mut child, &signal_relay, &mut tracker);
    let terminal_result = foreground_terminal.restore();
    let status = match status_result {
        Ok(status) => status,
        Err(error) => {
            let tracker_error = tracker.terminate().err();
            temp_directory.preserve();
            return Err(tracker_error.unwrap_or(error));
        }
    };

    if let Err(error) = tracker.terminate() {
        // Descendants may still be using their writable directory. Preserve it
        // when supervision fails instead of deleting files underneath them.
        temp_directory.preserve();
        return Err(error);
    }

    terminal_result?;
    Ok(exit_code(status))
}

fn wait_for_root(
    child: &mut std::process::Child,
    signal_relay: &SignalRelay,
    tracker: &mut DescendantTracker,
) -> Result<ExitStatus, String> {
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Ok(status),
            Ok(None) => {
                let process_group = child.id() as libc::pid_t;
                if let Err(error) = signal_relay.relay_pending(process_group) {
                    let _ = kill_root(child);
                    return Err(error);
                }
                match tracker.wait_for_events(None) {
                    Ok(EventWait::RootExited) => {
                        // Reaping the direct child produces its NOTE_REAP event;
                        // waiting on kqueue again here would deadlock.
                        return child.wait().map_err(|error| {
                            format!("failed to wait for `{SANDBOX_EXEC}`: {error}")
                        });
                    }
                    Ok(EventWait::Events | EventWait::TimedOut) => {}
                    Err(error) => {
                        let _ = kill_root(child);
                        return Err(error);
                    }
                }
            }
            Err(error) => {
                let _ = kill_root(child);
                return Err(format!("failed to wait for `{SANDBOX_EXEC}`: {error}"));
            }
        }
    }
}

fn kill_root(child: &mut std::process::Child) -> Result<ExitStatus, String> {
    let result = unsafe { libc::kill(-(child.id() as libc::pid_t), libc::SIGKILL) };
    if result != 0 {
        let kill_error = std::io::Error::last_os_error();
        match child.try_wait() {
            Ok(Some(status)) => return Ok(status),
            Ok(None) => {
                return Err(format!(
                    "failed to terminate the `{SANDBOX_EXEC}` process group: {kill_error}"
                ));
            }
            Err(wait_error) => {
                return Err(format!(
                    "failed to terminate the `{SANDBOX_EXEC}` process group: \
                     {kill_error}; additionally failed to read its status: {wait_error}"
                ));
            }
        }
    }
    child
        .wait()
        .map_err(|error| format!("failed to wait for terminated `{SANDBOX_EXEC}`: {error}"))
}

struct TemporaryDirectory {
    path: PathBuf,
    remove_on_drop: bool,
}

impl TemporaryDirectory {
    fn new() -> Result<Self, String> {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|error| format!("failed to read the system clock: {error}"))?
            .as_nanos();
        let path =
            std::env::temp_dir().join(format!("mcp-console-tmp-{}-{unique}", std::process::id()));

        DirBuilder::new()
            .mode(0o700)
            .create(&path)
            .map_err(|error| {
                format!(
                    "failed to create temporary directory `{}`: {error}",
                    path.display()
                )
            })?;

        let path = path.canonicalize().map_err(|error| {
            format!(
                "failed to resolve temporary directory `{}`: {error}",
                path.display()
            )
        })?;
        Ok(Self {
            path,
            remove_on_drop: true,
        })
    }

    fn path(&self) -> &Path {
        &self.path
    }

    fn preserve(&mut self) {
        self.remove_on_drop = false;
    }
}

impl Drop for TemporaryDirectory {
    fn drop(&mut self) {
        // Cleanup must not replace the child status if it changed directory modes.
        if self.remove_on_drop {
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}

// `sandbox-exec -DNAME=VALUE` supplies values for `(param "NAME")` in the SBPL.
fn parameter_definition(name: &str, path: &Path) -> OsString {
    let mut argument = OsString::from("-D");
    argument.push(name);
    argument.push("=");
    argument.push(path);
    argument
}

fn exit_code(status: ExitStatus) -> ExitCode {
    let code = status
        .code()
        .unwrap_or_else(|| 128 + status.signal().unwrap_or(1));
    ExitCode::from(code as u8)
}
