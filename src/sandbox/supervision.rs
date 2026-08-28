#[path = "supervision/file_descriptors.rs"]
mod file_descriptors;
#[path = "supervision/job_control.rs"]
mod job_control;
#[path = "supervision/process.rs"]
mod process;
#[path = "supervision/process_tracker.rs"]
mod process_tracker;

use self::file_descriptors::configure as configure_file_descriptors;
use self::job_control::{ForegroundTerminal, SignalRelay};
use self::process_tracker::{DescendantTracker, EventWait};
use super::platform;
use std::process::{Child, Command, ExitCode, ExitStatus};
use std::time::Duration;

pub(super) fn configure_command(command: &mut Command) -> Result<(), String> {
    configure_file_descriptors(command, Vec::new())
}

pub(super) fn status(
    mut sandbox_command: Command,
    temporary_directory: platform::TemporaryDirectory,
) -> Result<ExitCode, String> {
    configure_command(&mut sandbox_command)?;

    let signal_relay = SignalRelay::install()?;
    let mut foreground_terminal = ForegroundTerminal::detect();
    signal_relay.configure_child(&mut sandbox_command, foreground_terminal.descriptor());

    let mut child = sandbox_command
        .spawn()
        .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;
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
            preserve(temporary_directory);
            return Err(error);
        }
    };

    if let Err(error) = wait_for_root_exit(&child, &signal_relay, &mut tracker) {
        preserve(temporary_directory);
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
    let terminal_result = foreground_terminal.restore();

    // Keep the exited root waitable through descendant teardown. Its process
    // table entry reserves the process-group ID for fallback group signaling.
    if let Err(error) = tracker.terminate_after_root_exit() {
        preserve(temporary_directory);
        let mut error = match kill_root(&mut child) {
            Ok(_) => error,
            Err(kill_error) => additional_error(error, kill_error),
        };
        if let Err(terminal_error) = terminal_result {
            error = additional_error(error, terminal_error);
        }
        return Err(error);
    }

    let status = match child.wait() {
        Ok(status) => status,
        Err(wait_error) => {
            let error = format!(
                "failed to wait for `{}`: {wait_error}",
                platform::SANDBOX_EXEC
            );
            return Err(match terminal_result {
                Ok(()) => error,
                Err(terminal_error) => additional_error(error, terminal_error),
            });
        }
    };
    terminal_result?;
    Ok(platform::exit_code(status))
}

fn wait_for_root_exit(
    child: &Child,
    signal_relay: &SignalRelay,
    tracker: &mut DescendantTracker,
) -> Result<(), String> {
    loop {
        if platform::wait_for_process_exit_without_reaping(child.id(), Duration::ZERO).map_err(
            |error| {
                format!(
                    "failed to inspect `{}` exit status: {error}",
                    platform::SANDBOX_EXEC
                )
            },
        )? {
            return Ok(());
        }

        let process_group = child.id() as libc::pid_t;
        signal_relay.relay_pending(process_group)?;
        match tracker.wait_for_events(None) {
            Ok(EventWait::RootExited) => return Ok(()),
            Ok(EventWait::Events | EventWait::TimedOut) => {}
            Err(error) => return Err(error),
        }
    }
}

fn additional_error(primary: String, additional: String) -> String {
    format!("{primary}; additionally, {additional}")
}

// Callers retain the direct child waitably until after this function signals
// its process group, so its PID and process-group ID cannot be reused.
fn kill_root(child: &mut Child) -> Result<ExitStatus, String> {
    let result = unsafe { libc::kill(-(child.id() as libc::pid_t), libc::SIGKILL) };
    if result != 0 {
        let kill_error = std::io::Error::last_os_error();
        match child.try_wait() {
            Ok(Some(status)) => return Ok(status),
            Ok(None) => {
                return Err(format!(
                    "failed to terminate the `{}` process group: {kill_error}",
                    platform::SANDBOX_EXEC
                ));
            }
            Err(wait_error) => {
                return Err(format!(
                    "failed to terminate the `{}` process group: {kill_error}; \
                     additionally failed to read its status: {wait_error}",
                    platform::SANDBOX_EXEC
                ));
            }
        }
    }
    child.wait().map_err(|error| {
        format!(
            "failed to wait for terminated `{}`: {error}",
            platform::SANDBOX_EXEC
        )
    })
}

fn preserve(temporary_directory: platform::TemporaryDirectory) {
    // A live descendant may still be using this path. Deliberately leak the
    // guard on containment failure instead of deleting files underneath it.
    std::mem::forget(temporary_directory);
}
