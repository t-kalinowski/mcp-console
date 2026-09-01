use super::process_tracker::DescendantTracker;
use super::{
    PROCESS_RETIREMENT_GRACE, SandboxManager, additional_error, preserve, stop_direct_child,
};
use crate::sandbox::{
    CRASH_MANAGER_CLEANUP_TIMEOUT, TARGET_GATE_RELEASE, file_descriptors, platform,
};
use std::io::Write as _;
use std::os::fd::AsRawFd as _;
use std::os::unix::net::UnixStream;
use std::process::{Child, Command, ExitCode};

pub(super) fn status(
    mut sandbox_command: Command,
    temporary_directory: platform::TemporaryDirectory,
    target_gate: UnixStream,
    mut launcher_gate: UnixStream,
) -> Result<ExitCode, String> {
    let target_gate_descriptor = target_gate.as_raw_fd();
    // This path creates the release channel before starting any threads, so a
    // parent-side snapshot can close every inherited descriptor except its
    // child endpoint.
    file_descriptors::close_unlisted_except(&mut sandbox_command, target_gate_descriptor)?;
    let mut child = sandbox_command
        .spawn()
        .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;
    // Only the sandboxed wrapper retains the child endpoint after spawn.
    drop(target_gate);

    let tracker = match DescendantTracker::start(child.id() as libc::pid_t) {
        Ok(tracker) => tracker,
        Err(failure) => {
            let error = failure.retire(PROCESS_RETIREMENT_GRACE);
            let error = stop_direct_child(&mut child, error);
            preserve(temporary_directory);
            return Err(error);
        }
    };
    let manager_wakeup = match tracker.register_observer_wakeup() {
        Ok(manager_wakeup) => manager_wakeup,
        Err(error) => {
            let error = retire_after_manager_start_failure(tracker, &mut child, error);
            preserve(temporary_directory);
            return Err(error);
        }
    };
    let mut manager = match SandboxManager::start_for_standalone(
        child.id(),
        temporary_directory.path(),
        CRASH_MANAGER_CLEANUP_TIMEOUT,
        manager_wakeup,
    ) {
        Ok(manager) => manager,
        Err(error) => {
            let error = retire_after_manager_start_failure(tracker, &mut child, error);
            preserve(temporary_directory);
            return Err(error);
        }
    };

    let startup_error = launcher_gate
        .write_all(&[TARGET_GATE_RELEASE])
        .map_err(|error| format!("failed to release sandbox target startup gate: {error}"))
        .err();
    drop(launcher_gate);

    // The launcher keeps its original observer as the authority for normal
    // cleanup. Mark that cleanup before its first termination pass so an owner
    // crash during an uncertain local retirement makes the independent manager
    // preserve, rather than remove, the private directory.
    let retirement = match startup_error {
        None => tracker.supervise(PROCESS_RETIREMENT_GRACE, || {
            let _ = manager.begin_retirement();
        }),
        Some(error) => {
            let _ = manager.begin_retirement();
            match tracker.terminate(false, PROCESS_RETIREMENT_GRACE) {
                Ok(()) => Err(error),
                Err(cleanup_error) => Err(additional_error(error, cleanup_error)),
            }
        }
    };
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
    Ok(platform::exit_code(status.expect(
        "successful standalone retirement should retain the root status",
    )))
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
