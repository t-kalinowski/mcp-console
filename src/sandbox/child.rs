use std::process::{Child, ExitStatus};
use std::time::Duration;

use super::platform;

const DIRECT_CHILD_CLEANUP_TIMEOUT: Duration = Duration::from_secs(1);

pub(super) struct UnmanagedChildCleanup {
    pub(super) error: String,
    pub(super) identity_released: bool,
}

/// Stops the pinned sandbox process group, falls back to the direct child, and
/// reaps the direct child without releasing its PID between those operations.
pub(super) fn terminate_unmanaged_child(
    child: &mut Child,
    mut error: String,
) -> UnmanagedChildCleanup {
    if let Err(group_error) = platform::kill_process_group(child.id()) {
        error = append_retirement_error(
            error,
            format!(
                "failed to stop `{}` process group: {group_error}",
                platform::SANDBOX_EXEC
            ),
        );
    }

    let mut exited =
        platform::wait_for_process_exit_without_reaping(child.id(), DIRECT_CHILD_CLEANUP_TIMEOUT)
            .unwrap_or(false);
    if !exited
        && let Err(kill_error) = child.kill()
        && kill_error.raw_os_error() != Some(libc::ESRCH)
    {
        error = append_retirement_error(
            error,
            format!("failed to stop `{}`: {kill_error}", platform::SANDBOX_EXEC),
        );
    }
    if !exited {
        match platform::wait_for_process_exit_without_reaping(
            child.id(),
            DIRECT_CHILD_CLEANUP_TIMEOUT,
        ) {
            Ok(observed) => exited = observed,
            Err(wait_error) => {
                return UnmanagedChildCleanup {
                    error: append_retirement_error(
                        error,
                        format!(
                            "failed to wait for `{}` to exit before reaping it: {wait_error}",
                            platform::SANDBOX_EXEC
                        ),
                    ),
                    identity_released: wait_error.raw_os_error() == Some(libc::ECHILD),
                };
            }
        }
    }
    if !exited {
        return UnmanagedChildCleanup {
            error: append_retirement_error(
                error,
                format!(
                    "failed to reap `{}`: process remained live after {} ms",
                    platform::SANDBOX_EXEC,
                    DIRECT_CHILD_CLEANUP_TIMEOUT.as_millis()
                ),
            ),
            identity_released: false,
        };
    }
    match child.wait() {
        Ok(_) => UnmanagedChildCleanup {
            error,
            identity_released: true,
        },
        Err(wait_error) => UnmanagedChildCleanup {
            error: append_retirement_error(
                error,
                format!("failed to reap `{}`: {wait_error}", platform::SANDBOX_EXEC),
            ),
            identity_released: wait_error.raw_os_error() == Some(libc::ECHILD),
        },
    }
}

/// Stops and reaps the standalone command while its waitable PID still pins the
/// process-group identity. This retains the standalone launcher's historical
/// diagnostics for partial group-termination failures.
pub(super) fn terminate_standalone_root(
    child: &mut Child,
    timeout: Duration,
) -> Result<ExitStatus, String> {
    let group_result = unsafe { libc::kill(-(child.id() as libc::pid_t), libc::SIGKILL) };
    let group_error = (group_result != 0)
        .then(std::io::Error::last_os_error)
        .filter(|error| error.raw_os_error() != Some(libc::ESRCH));

    match child.try_wait() {
        Ok(Some(status)) => {
            return group_error.map_or(Ok(status), |group_error| {
                Err(format!(
                    "process-group termination also failed: {group_error}"
                ))
            });
        }
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
        return Err(format!(
            "failed to terminate direct {} process: {error}{}",
            platform::SANDBOX_EXEC,
            group_error_suffix(&group_error)
        ));
    }

    let exited =
        platform::wait_for_process_exit_without_reaping(child.id(), timeout).map_err(|error| {
            format!(
                "failed to inspect `{}` exit status: {error}{}",
                platform::SANDBOX_EXEC,
                group_error_suffix(&group_error)
            )
        })?;
    if !exited {
        return Err(format!(
            "timed out waiting for terminated {}{}",
            platform::SANDBOX_EXEC,
            group_error_suffix(&group_error)
        ));
    }

    let status = child.wait().map_err(|error| {
        format!(
            "failed to wait for terminated {}: {error}{}",
            platform::SANDBOX_EXEC,
            group_error_suffix(&group_error)
        )
    })?;
    group_error.map_or(Ok(status), |group_error| {
        Err(format!(
            "process-group termination also failed: {group_error}"
        ))
    })
}

fn group_error_suffix(group_error: &Option<std::io::Error>) -> String {
    group_error
        .as_ref()
        .map(|error| format!("; process-group termination also failed: {error}"))
        .unwrap_or_default()
}

pub(super) fn append_retirement_error(prior: String, error: String) -> String {
    format!("{prior}; additionally {error}")
}
