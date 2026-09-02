use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, ExitStatus};
use std::time::Duration;

use super::command::{SandboxedChild, SandboxedChildRetirement};
use super::platform;

const DIRECT_CHILD_CLEANUP_TIMEOUT: Duration = Duration::from_secs(1);

impl SandboxedChild {
    #[allow(dead_code, reason = "used by spawned callers with piped stdin")]
    pub(crate) fn take_stdin(&mut self) -> Option<ChildStdin> {
        self.child.stdin.take()
    }

    #[allow(dead_code, reason = "used by spawned callers with piped stdout")]
    pub(crate) fn take_stdout(&mut self) -> Option<ChildStdout> {
        self.child.stdout.take()
    }

    #[allow(dead_code, reason = "used by spawned callers with piped stderr")]
    pub(crate) fn take_stderr(&mut self) -> Option<ChildStderr> {
        self.child.stderr.take()
    }

    /// Waits at most `timeout` for the direct sandbox process to exit without
    /// reaping it.
    ///
    /// Retaining the waitable child pins its PID, which is also its process-group
    /// ID, until sandbox-lifetime cleanup completes and the direct process is
    /// reaped.
    pub(crate) fn wait_timeout_without_reaping(&self, timeout: Duration) -> Result<bool, String> {
        match &self.retirement {
            SandboxedChildRetirement::Managed | SandboxedChildRetirement::Unmanaged { .. } => {}
            SandboxedChildRetirement::Retired { .. } => return Ok(true),
        }
        platform::wait_for_process_exit_without_reaping(self.child.id(), timeout).map_err(|error| {
            format!(
                "failed to wait for `{}` to exit without reaping it: {error}",
                platform::SANDBOX_EXEC
            )
        })
    }

    /// Stops the root and observed descendants through the host-side manager or
    /// its recovery monitor, then reaps the direct sandbox process.
    pub(crate) fn force_stop(&mut self) -> Result<(), String> {
        match &self.retirement {
            SandboxedChildRetirement::Managed => {}
            SandboxedChildRetirement::Unmanaged { error } => {
                return self.retry_unmanaged_retirement(error.clone());
            }
            SandboxedChildRetirement::Retired { error } => {
                return stored_retirement_result(error);
            }
        }

        let manager = self
            .manager
            .take()
            .expect("active sandbox child should retain its lifetime manager");
        match manager.stop() {
            Ok(()) => self.reap_managed_child(),
            Err(error) => self.retry_unmanaged_retirement(error),
        }
    }

    fn reap_managed_child(&mut self) -> Result<(), String> {
        match self.child.wait() {
            Ok(_) => {
                self.retirement = SandboxedChildRetirement::Retired { error: None };
                Ok(())
            }
            Err(wait_error) => {
                let identity_released = wait_error.raw_os_error() == Some(libc::ECHILD);
                let error = format!(
                    "failed to reap stopped `{}`: {wait_error}",
                    platform::SANDBOX_EXEC
                );
                self.retirement = if identity_released {
                    SandboxedChildRetirement::Retired {
                        error: Some(error.clone()),
                    }
                } else {
                    SandboxedChildRetirement::Unmanaged {
                        error: error.clone(),
                    }
                };
                Err(error)
            }
        }
    }

    fn retry_unmanaged_retirement(&mut self, error: String) -> Result<(), String> {
        let cleanup = terminate_unmanaged_child(&mut self.child, error);
        let error = cleanup.error;
        self.retirement = if cleanup.identity_released {
            SandboxedChildRetirement::Retired {
                error: Some(error.clone()),
            }
        } else {
            SandboxedChildRetirement::Unmanaged {
                error: error.clone(),
            }
        };
        Err(error)
    }
}

impl Drop for SandboxedChild {
    fn drop(&mut self) {
        let _ = self.force_stop();
    }
}

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

fn stored_retirement_result(error: &Option<String>) -> Result<(), String> {
    error.as_ref().map_or(Ok(()), |error| Err(error.clone()))
}

/// Kills every other live member of the caller's sandbox process group.
///
/// The sandbox relay remains alive to reap its direct worker and flush its
/// protocol output. Fail fast unless the caller is the process-group leader so
/// this cannot accidentally target an inherited server process group.
pub(crate) fn force_stop_process_group_members_except_self() -> Result<(), String> {
    let process_id = std::process::id();
    // SAFETY: `getpgrp` has no error return and reads the calling process's
    // current process-group ID.
    let process_group_id = unsafe { libc::getpgrp() };
    if process_group_id != process_id as libc::pid_t {
        return Err("sandbox relay is not its process-group leader".to_string());
    }

    platform::kill_process_group_members_except(process_id, process_id)
        .map_err(|error| format!("failed to stop sandbox process-group members: {error}"))
}
