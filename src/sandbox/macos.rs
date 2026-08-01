mod file_descriptors;
mod job_control;
mod process;
mod process_tracker;

use self::file_descriptors::configure as configure_file_descriptors;
use self::job_control::{ForegroundTerminal, SignalRelay};
use self::process_tracker::{DescendantTracker, EventWait};
use std::ffi::OsString;
use std::fs::{self, DirBuilder};
use std::os::fd::{AsRawFd as _, OwnedFd};
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitCode, ExitStatus};
use std::time::{SystemTime, UNIX_EPOCH};

pub(super) const SANDBOX_EXEC: &str = "/usr/bin/sandbox-exec";
const POLICY: &str = include_str!("read_only_policy.sbpl");

pub(super) fn sandboxed_command() -> Result<(Command, TemporaryDirectory), String> {
    let temporary_directory = TemporaryDirectory::new()?;
    let mut launcher = Command::new(SANDBOX_EXEC);
    launcher
        .arg("-p")
        .arg(POLICY)
        .arg(parameter_definition(
            "TEMP_DIRECTORY",
            temporary_directory.path(),
        ))
        .arg("--");

    Ok((launcher, temporary_directory))
}

pub(super) fn spawn_command(
    mut command: Command,
    inherited_descriptors: Vec<OwnedFd>,
) -> Result<Child, String> {
    let allowed = inherited_descriptors
        .iter()
        .map(|descriptor| descriptor.as_raw_fd())
        .collect();
    configure_file_descriptors(&mut command, allowed)?;
    let child = command.spawn();
    drop(inherited_descriptors);
    child.map_err(|error| format!("failed to launch `{SANDBOX_EXEC}`: {error}"))
}

pub(super) fn supervised_status(
    mut sandbox_command: Command,
    mut temp_directory: TemporaryDirectory,
    inherited_descriptors: Vec<OwnedFd>,
) -> Result<ExitCode, String> {
    let signal_relay = SignalRelay::install()?;
    let mut foreground_terminal = ForegroundTerminal::detect();
    signal_relay.configure_child(&mut sandbox_command, foreground_terminal.descriptor());

    let mut child = spawn_command(sandbox_command, inherited_descriptors)?;
    let mut tracker = match DescendantTracker::start(child.id() as libc::pid_t, &signal_relay) {
        Ok(tracker) => tracker,
        Err(error) => {
            let error = match kill_root(&mut child) {
                Ok(_) => error,
                Err(kill_error) => additional_error(error, kill_error),
            };
            let error = match foreground_terminal.restore() {
                Ok(()) => error,
                Err(terminal_error) => additional_error(error, terminal_error),
            };
            temp_directory.preserve();
            return Err(error);
        }
    };

    let status = match wait_for_root(&mut child, &signal_relay, &mut tracker) {
        Ok(status) => status,
        Err(error) => {
            temp_directory.preserve();
            let root_result = kill_root(&mut child);
            let root_reaped = root_result.is_ok();
            let mut error = match root_result {
                Ok(_) => error,
                Err(kill_error) => additional_error(error, kill_error),
            };
            if let Err(terminal_error) = foreground_terminal.restore() {
                error = additional_error(error, terminal_error);
            }
            if root_reaped && let Err(tracker_error) = tracker.terminate_after_root_exit() {
                error = additional_error(error, tracker_error);
            }
            return Err(error);
        }
    };
    let terminal_result = foreground_terminal.restore();

    if let Err(error) = tracker.terminate_after_root_exit() {
        // Descendants may still be using their writable directory. Preserve it
        // when supervision fails instead of deleting files underneath them.
        temp_directory.preserve();
        let mut error = match kill_root(&mut child) {
            Ok(_) => error,
            Err(kill_error) => additional_error(error, kill_error),
        };
        if let Err(terminal_error) = terminal_result {
            error = additional_error(error, terminal_error);
        }
        return Err(error);
    }

    terminal_result?;
    Ok(exit_code(status))
}

fn wait_for_root(
    child: &mut Child,
    signal_relay: &SignalRelay,
    tracker: &mut DescendantTracker,
) -> Result<ExitStatus, String> {
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Ok(status),
            Ok(None) => {
                let process_group = child.id() as libc::pid_t;
                signal_relay.relay_pending(process_group)?;
                match tracker.wait_for_events(None) {
                    Ok(EventWait::RootExited) => {
                        // Reaping the direct child produces its NOTE_REAP event;
                        // waiting on kqueue again here would deadlock.
                        return child.wait().map_err(|error| {
                            format!("failed to wait for `{SANDBOX_EXEC}`: {error}")
                        });
                    }
                    Ok(EventWait::Events | EventWait::TimedOut) => {}
                    Err(error) => return Err(error),
                }
            }
            Err(error) => return Err(format!("failed to wait for `{SANDBOX_EXEC}`: {error}")),
        }
    }
}

fn additional_error(primary: String, additional: String) -> String {
    format!("{primary}; additionally, {additional}")
}

fn kill_root(child: &mut Child) -> Result<ExitStatus, String> {
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

pub(super) struct TemporaryDirectory {
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

    pub(super) fn path(&self) -> &Path {
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
