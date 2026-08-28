#[path = "supervision/file_descriptors.rs"]
mod file_descriptors;
#[path = "supervision/guardian.rs"]
mod guardian;
#[path = "supervision/job_control.rs"]
mod job_control;
#[path = "supervision/process.rs"]
mod process;
#[path = "supervision/process_tracker.rs"]
mod process_tracker;

use self::file_descriptors::configure as configure_file_descriptors;
use self::guardian::Guardian;
use self::job_control::{ForegroundTerminal, SignalRelay};
pub(crate) use self::process_tracker::DescendantTracker;
use self::process_tracker::EventWait;
use super::platform;
use std::os::fd::RawFd;
use std::process::{Child, Command, ExitCode, ExitStatus};
use std::time::Duration;

pub(super) fn configure_command(
    command: &mut Command,
    inherited_descriptors: Vec<RawFd>,
) -> Result<(), String> {
    configure_file_descriptors(command, inherited_descriptors)
}

pub(super) fn run_guardian() -> Result<(), String> {
    guardian::run()
}

pub(super) fn status(
    mut sandbox_command: Command,
    temporary_directory: platform::TemporaryDirectory,
) -> Result<ExitCode, String> {
    configure_command(&mut sandbox_command, Vec::new())?;

    let mut guardian = Guardian::spawn()?;
    let signal_relay = SignalRelay::install()?;
    let mut foreground_terminal = ForegroundTerminal::detect();
    signal_relay.configure_child(&mut sandbox_command, foreground_terminal.descriptor());

    let mut child = sandbox_command
        .spawn()
        .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;
    let mut tracker = match DescendantTracker::start_with_signal_relay(
        child.id() as libc::pid_t,
        &signal_relay,
    ) {
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

    if let Err(error) = guardian.observe(child.id(), temporary_directory.path()) {
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
        if let Err(guardian_error) = guardian.finish(true) {
            error = additional_error(error, guardian_error);
        }
        return Err(error);
    }

    temporary_directory.relinquish();
    if let Err(error) = guardian.commit() {
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
        if let Err(guardian_error) = guardian.finish(true) {
            error = additional_error(error, guardian_error);
        }
        return Err(error);
    }

    if let Err(error) = wait_for_root_exit(&child, &signal_relay, &mut tracker) {
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
        if let Err(guardian_error) = guardian.finish(true) {
            error = additional_error(error, guardian_error);
        }
        return Err(error);
    }
    let terminal_result = foreground_terminal.restore();

    // Keep the exited root waitable through descendant teardown. Its process
    // table entry reserves the process-group ID for fallback group signaling.
    if let Err(error) = tracker.terminate_after_root_exit() {
        let mut error = match kill_root(&mut child) {
            Ok(_) => error,
            Err(kill_error) => additional_error(error, kill_error),
        };
        if let Err(terminal_error) = terminal_result {
            error = additional_error(error, terminal_error);
        }
        if let Err(guardian_error) = guardian.finish(true) {
            error = additional_error(error, guardian_error);
        }
        return Err(error);
    }

    let status = match child.wait() {
        Ok(status) => status,
        Err(wait_error) => {
            let mut error = format!(
                "failed to wait for `{}`: {wait_error}",
                platform::SANDBOX_EXEC
            );
            if let Err(terminal_error) = terminal_result {
                error = additional_error(error, terminal_error);
            }
            if let Err(guardian_error) = guardian.finish(false) {
                error = additional_error(error, guardian_error);
            }
            return Err(error);
        }
    };
    let guardian_result = guardian.finish(false);
    match (terminal_result, guardian_result) {
        (Ok(()), Ok(())) => Ok(platform::exit_code(status)),
        (Err(error), Ok(())) | (Ok(()), Err(error)) => Err(error),
        (Err(error), Err(guardian_error)) => Err(additional_error(error, guardian_error)),
    }
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
    let group_result = unsafe { libc::kill(-(child.id() as libc::pid_t), libc::SIGKILL) };
    let group_error = (group_result != 0)
        .then(std::io::Error::last_os_error)
        .filter(|error| error.raw_os_error() != Some(libc::ESRCH));

    match child.try_wait() {
        Ok(Some(status)) => return Ok(status),
        Ok(None) => {}
        Err(error) => {
            return Err(format!(
                "failed to read {} status during termination: {error}",
                platform::SANDBOX_EXEC
            ));
        }
    }

    if let Err(error) = child.kill()
        && error.raw_os_error() != Some(libc::ESRCH)
    {
        let group_error = group_error
            .map(|group_error| format!("; process-group termination also failed: {group_error}"))
            .unwrap_or_default();
        return Err(format!(
            "failed to terminate direct {} process: {error}{group_error}",
            platform::SANDBOX_EXEC
        ));
    }

    child.wait().map_err(|error| {
        let group_error = group_error
            .map(|group_error| format!("; process-group termination also failed: {group_error}"))
            .unwrap_or_default();
        format!(
            "failed to wait for terminated {}: {error}{group_error}",
            platform::SANDBOX_EXEC
        )
    })
}

fn preserve(temporary_directory: platform::TemporaryDirectory) {
    // A live descendant may still be using this path. Deliberately leak the
    // guard on containment failure instead of deleting files underneath it.
    temporary_directory.preserve();
}
