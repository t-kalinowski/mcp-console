#[path = "supervision/process.rs"]
mod process;
#[path = "supervision/process_retirement.rs"]
mod process_retirement;
#[path = "supervision/process_tracker.rs"]
mod process_tracker;
#[path = "supervision/process_tree.rs"]
mod process_tree;

use super::{file_descriptors, platform};
use std::process::{Child, Command, ExitCode};
use std::time::Duration;

const PROCESS_REAP_GRACE: Duration = Duration::from_secs(1);

/// A sandbox process tree observed from one direct root.
///
/// Darwin cannot atomically install a descendant observer at spawn time. A
/// descendant that becomes orphaned before the post-spawn root watch or a
/// corresponding fork observation remains outside this lifetime. Once a
/// process is observed, retirement follows its PID and start time across
/// process-group and session changes.
pub(crate) struct ObservedLifetime(process_tracker::DescendantTracker);

impl ObservedLifetime {
    pub(crate) fn start(root_pid: u32) -> Result<Self, String> {
        let root_pid = libc::pid_t::try_from(root_pid)
            .ok()
            .filter(|pid| *pid > 0)
            .ok_or_else(|| "sandbox process tracker received an invalid root PID".to_string())?;
        process_tracker::DescendantTracker::start(root_pid)
            .map(Self)
            .map_err(|failure| failure.retire(PROCESS_REAP_GRACE))
    }

    /// Stops the root and every descendant observed from it.
    pub(crate) fn stop(self) -> Result<(), String> {
        self.0.terminate(false, PROCESS_REAP_GRACE)
    }
}

pub(super) fn status(
    mut sandbox_command: Command,
    temporary_directory: platform::TemporaryDirectory,
) -> Result<ExitCode, String> {
    // The standalone path has no private transport descriptors, so a
    // parent-side snapshot is sufficient before it starts any threads.
    file_descriptors::close_unlisted(&mut sandbox_command)?;
    let mut child = sandbox_command
        .spawn()
        .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;

    let tracker = match process_tracker::DescendantTracker::start(child.id() as libc::pid_t) {
        Ok(tracker) => tracker,
        Err(failure) => {
            let error = failure.retire(PROCESS_REAP_GRACE);
            let error = stop_direct_child(&mut child, error);
            preserve(temporary_directory);
            return Err(error);
        }
    };
    if let Err(error) = tracker.supervise(PROCESS_REAP_GRACE) {
        let error = stop_direct_child(&mut child, error);
        preserve(temporary_directory);
        return Err(error);
    }

    let status = child
        .wait()
        .map_err(|error| format!("failed to wait for `{}`: {error}", platform::SANDBOX_EXEC))?;
    Ok(platform::exit_code(status))
}

pub(super) fn stop_direct_child(child: &mut Child, primary: String) -> String {
    let mut error = primary;
    match child.try_wait() {
        Ok(Some(_)) => return error,
        Ok(None) => {}
        Err(status_error) => {
            error = additional_error(
                error,
                format!(
                    "failed to inspect direct `{}` during cleanup: {status_error}",
                    platform::SANDBOX_EXEC
                ),
            );
        }
    }

    if let Err(kill_error) = child.kill()
        && kill_error.raw_os_error() != Some(libc::ESRCH)
    {
        return additional_error(
            error,
            format!(
                "failed to stop direct `{}` during cleanup: {kill_error}",
                platform::SANDBOX_EXEC
            ),
        );
    }

    match platform::wait_for_process_exit_without_reaping(child.id(), PROCESS_REAP_GRACE) {
        Ok(true) => {}
        Ok(false) => {
            return additional_error(
                error,
                format!(
                    "timed out waiting for direct `{}` to stop",
                    platform::SANDBOX_EXEC
                ),
            );
        }
        Err(wait_error) => {
            return additional_error(
                error,
                format!(
                    "failed to observe direct `{}` during cleanup: {wait_error}",
                    platform::SANDBOX_EXEC
                ),
            );
        }
    }
    if let Err(wait_error) = child.wait() {
        error = additional_error(
            error,
            format!(
                "failed to reap direct `{}` during cleanup: {wait_error}",
                platform::SANDBOX_EXEC
            ),
        );
    }
    error
}

fn additional_error(primary: String, additional: String) -> String {
    format!("{primary}; additionally, {additional}")
}

fn preserve(directory: platform::TemporaryDirectory) {
    // A live unobserved descendant may still use this path. Deliberately leak
    // the guard after cleanup failure rather than deleting files underneath it.
    std::mem::forget(directory);
}
