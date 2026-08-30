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
            let error = stop_direct_child(&mut child, error, PROCESS_REAP_GRACE);
            preserve(temporary_directory);
            return Err(error);
        }
    };
    if let Err(error) = tracker.supervise(PROCESS_REAP_GRACE) {
        let error = stop_direct_child(&mut child, error, PROCESS_REAP_GRACE);
        preserve(temporary_directory);
        return Err(error);
    }

    let status = child
        .wait()
        .map_err(|error| format!("failed to wait for `{}`: {error}", platform::SANDBOX_EXEC))?;
    Ok(platform::exit_code(status))
}

fn stop_direct_child(child: &mut Child, primary: String, timeout: Duration) -> String {
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

    match platform::wait_for_process_exit_without_reaping(child.id(), timeout) {
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
