#[path = "supervision/file_descriptors.rs"]
mod file_descriptors;
#[path = "supervision/job_control.rs"]
mod job_control;
#[path = "supervision/process.rs"]
mod process;
#[path = "supervision/process_tracker.rs"]
mod process_tracker;

use self::file_descriptors::configure as configure_file_descriptors;
use self::job_control::{LaunchMode, SignalRelay};
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

    let launch_mode = LaunchMode::detect();
    let signal_relay = SignalRelay::install(launch_mode)?;
    signal_relay.configure_child(&mut sandbox_command);

    let mut child = sandbox_command
        .spawn()
        .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;
    let mut tracker = match DescendantTracker::start(child.id() as libc::pid_t, &signal_relay) {
        Ok(tracker) => tracker,
        Err(error) => {
            let error = match kill_root(&mut child, launch_mode) {
                Ok(_) => error,
                Err(kill_error) => additional_error(error, kill_error),
            };
            preserve(temporary_directory);
            return Err(error);
        }
    };

    if let Err(error) = wait_for_root_exit(&child, &signal_relay, &mut tracker) {
        preserve(temporary_directory);
        let root_result = kill_root(&mut child, launch_mode);
        let root_reaped = root_result.is_ok();
        let mut error = match root_result {
            Ok(_) => error,
            Err(kill_error) => additional_error(error, kill_error),
        };
        if root_reaped && let Err(tracker_error) = tracker.terminate_after_root_exit() {
            error = additional_error(error, tracker_error);
        }
        return Err(error);
    }

    // Keep an isolated root waitable while its pinned process group and every
    // observed identity are retired. The group pass closes the fork-and-exit
    // window for same-group children that orphaned before NOTE_FORK was handled.
    // A terminal-attached root shares the shell's job group, so signalling that
    // group could terminate pipeline peers; only observed identities are retired.
    let group_result = if launch_mode.is_isolated() {
        platform::kill_process_group(child.id())
            .map_err(|error| format!("failed to stop the sandbox process group: {error}"))
    } else {
        Ok(())
    };
    let tracker_result = tracker.terminate_after_root_exit();
    let retirement_result = match (group_result, tracker_result) {
        (Ok(()), Ok(())) => Ok(()),
        (Err(error), Ok(())) | (Ok(()), Err(error)) => Err(error),
        (Err(error), Err(tracker_error)) => Err(additional_error(error, tracker_error)),
    };
    if let Err(error) = retirement_result {
        preserve(temporary_directory);
        let error = match kill_root(&mut child, launch_mode) {
            Ok(_) => error,
            Err(kill_error) => additional_error(error, kill_error),
        };
        return Err(error);
    }

    let child = super::SandboxedChild {
        child,
        retirement: super::SandboxedChildRetirement::Active,
        _temporary_directory: temporary_directory,
    };
    let status = child.wait()?;
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

        signal_relay.handle_pending(child.id() as libc::pid_t)?;
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

fn kill_root(child: &mut Child, launch_mode: LaunchMode) -> Result<ExitStatus, String> {
    let stop_result = if launch_mode.is_isolated() {
        platform::kill_process_group(child.id()).map_err(|error| {
            format!(
                "failed to terminate the `{}` process group: {error}",
                platform::SANDBOX_EXEC
            )
        })
    } else {
        child.kill().map_err(|error| {
            format!(
                "failed to terminate direct `{}` process: {error}",
                platform::SANDBOX_EXEC
            )
        })
    };

    if let Err(stop_error) = stop_result {
        match child.try_wait() {
            Ok(Some(status)) => return Ok(status),
            Ok(None) => return Err(stop_error),
            Err(wait_error) => {
                return Err(additional_error(
                    stop_error,
                    format!(
                        "failed to read `{}` status: {wait_error}",
                        platform::SANDBOX_EXEC
                    ),
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
