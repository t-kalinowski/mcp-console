use super::process_tracker::DescendantTracker;
use super::{
    PROCESS_RETIREMENT_GRACE, SandboxManager, additional_error, preserve, stop_direct_child,
};
use crate::sandbox::{CRASH_MANAGER_CLEANUP_TIMEOUT, file_descriptors, platform};
use std::process::{Child, Command, ExitCode};

pub(super) fn status(
    mut sandbox_command: Command,
    temporary_directory: platform::TemporaryDirectory,
) -> Result<ExitCode, String> {
    // The standalone path has no private transport descriptors, so a
    // parent-side snapshot is sufficient before it starts the manager monitor.
    file_descriptors::close_unlisted(&mut sandbox_command)?;
    let mut child = sandbox_command
        .spawn()
        .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;

    let tracker = match DescendantTracker::start(child.id() as libc::pid_t) {
        Ok(tracker) => tracker,
        Err(failure) => {
            let error = failure.retire(PROCESS_RETIREMENT_GRACE);
            let error = stop_direct_child(&mut child, error);
            preserve(temporary_directory);
            return Err(error);
        }
    };
    let mut manager = match SandboxManager::start(
        child.id(),
        temporary_directory.path(),
        CRASH_MANAGER_CLEANUP_TIMEOUT,
    ) {
        Ok(manager) => manager,
        Err(error) => {
            let error = retire_after_manager_start_failure(tracker, &mut child, error);
            preserve(temporary_directory);
            return Err(error);
        }
    };

    // The launcher keeps its original observer as the authority for normal
    // cleanup. Mark that cleanup before its first termination pass so an owner
    // crash during an uncertain local retirement makes the independent manager
    // preserve, rather than remove, the private directory.
    let retirement = tracker.supervise(PROCESS_RETIREMENT_GRACE, || {
        let _ = manager.begin_retirement();
    });
    let mut error = retirement.err();
    let manager_prepared = manager.prepare_finish();

    let status = if let Some(retirement_error) = error.take() {
        error = Some(stop_direct_child(&mut child, retirement_error));
        None
    } else {
        match child.wait() {
            Ok(status) => Some(status),
            Err(wait_error) => {
                error = Some(format!(
                    "failed to wait for `{}`: {wait_error}",
                    platform::SANDBOX_EXEC
                ));
                None
            }
        }
    };

    if let Err(manager_error) = manager.finish(error.is_some() || !manager_prepared) {
        error = Some(match error {
            Some(error) => additional_error(error, manager_error),
            None => manager_error,
        });
    }
    if let Some(error) = error {
        preserve(temporary_directory);
        return Err(error);
    }

    drop(temporary_directory);
    Ok(platform::exit_code(
        status.expect("successful standalone retirement should retain the root status"),
    ))
}

fn retire_after_manager_start_failure(
    tracker: DescendantTracker,
    child: &mut Child,
    error: String,
) -> String {
    let error = match tracker.terminate(false, PROCESS_RETIREMENT_GRACE) {
        Ok(()) => error,
        Err(retirement_error) => additional_error(error, retirement_error),
    };
    stop_direct_child(child, error)
}
