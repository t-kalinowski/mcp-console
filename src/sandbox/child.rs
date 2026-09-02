use std::process::{ChildStderr, ChildStdin, ChildStdout};
use std::time::Duration;

use super::command::{SandboxedChild, SandboxedChildRetirement};
use super::platform;

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
            SandboxedChildRetirement::Retired { .. } => return Ok(true),
            SandboxedChildRetirement::Failed { error } => return Err(error.clone()),
            SandboxedChildRetirement::Active | SandboxedChildRetirement::AwaitingReap { .. } => {}
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
            SandboxedChildRetirement::Retired { error } => return stored_retirement_result(error),
            SandboxedChildRetirement::AwaitingReap { error } => {
                return self.reap_after_stop(error.clone());
            }
            SandboxedChildRetirement::Failed { error } => return Err(error.clone()),
            SandboxedChildRetirement::Active => {}
        }

        let manager = self
            .manager
            .take()
            .expect("active sandbox child should retain its lifetime manager");
        let mut error = manager.stop().err();
        if error.is_some() {
            if let Err(group_error) = platform::kill_process_group(self.child.id()) {
                error = Some(append_retirement_error(
                    error,
                    format!(
                        "failed to stop `{}` process group: {group_error}",
                        platform::SANDBOX_EXEC
                    ),
                ));
            }
            if let Err(kill_error) = self.child.kill()
                && kill_error.raw_os_error() != Some(libc::ESRCH)
            {
                let error = append_retirement_error(
                    error,
                    format!(
                        "failed to stop direct `{}` process: {kill_error}",
                        platform::SANDBOX_EXEC
                    ),
                );
                self.retirement = SandboxedChildRetirement::Failed {
                    error: error.clone(),
                };
                return Err(error);
            }
        }

        self.retirement = SandboxedChildRetirement::AwaitingReap {
            error: error.clone(),
        };
        self.reap_after_stop(error)
    }

    fn reap_after_stop(&mut self, prior_error: Option<String>) -> Result<(), String> {
        match self.child.wait() {
            Ok(_) => {
                self.retirement = SandboxedChildRetirement::Retired {
                    error: prior_error.clone(),
                };
                stored_retirement_result(&prior_error)
            }
            Err(wait_error) => {
                let identity_released = wait_error.raw_os_error() == Some(libc::ECHILD);
                let error = append_retirement_error(
                    prior_error,
                    format!(
                        "failed to reap stopped `{}`: {wait_error}",
                        platform::SANDBOX_EXEC
                    ),
                );
                if identity_released {
                    self.retirement = SandboxedChildRetirement::Retired {
                        error: Some(error.clone()),
                    };
                } else {
                    self.retirement = SandboxedChildRetirement::AwaitingReap {
                        error: Some(error.clone()),
                    };
                }
                Err(error)
            }
        }
    }
}

impl Drop for SandboxedChild {
    fn drop(&mut self) {
        let _ = self.force_stop();
    }
}

pub(super) fn append_retirement_error(prior: Option<String>, error: String) -> String {
    prior.map_or(error.clone(), |prior| {
        format!("{prior}; additionally {error}")
    })
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
