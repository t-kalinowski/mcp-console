use std::process::{ChildStderr, ChildStdin, ChildStdout};
use std::time::Duration;

use super::command::{SandboxedChild, SandboxedChildRetirement};
use super::{platform, supervision};

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
    /// Retaining the waitable child pins its PID while observed-descendant
    /// retirement runs. Callers that requested `new_process_group` also retain
    /// the group identity for fallback cleanup until `force_stop` reaps the root.
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

    /// Stops the root and descendants observed across process-group and session
    /// changes, waits for committed manager cleanup, reaps the direct sandbox
    /// process, then commits the temporary-directory disposition.
    ///
    /// Process-group cleanup always runs as a backstop for unobserved same-group
    /// forks; direct-child cleanup also runs after any retirement error. The
    /// private temporary directory is preserved on any cleanup error or manager
    /// cleanup timeout because an unobserved process may remain live.
    pub(crate) fn force_stop(&mut self) -> Result<(), String> {
        match &self.retirement {
            SandboxedChildRetirement::Retired { error } => return stored_retirement_result(error),
            SandboxedChildRetirement::AwaitingReap { error } => {
                return self.reap_after_stop(error.clone());
            }
            SandboxedChildRetirement::Failed { error } => return Err(error.clone()),
            SandboxedChildRetirement::Active => {}
        }

        if let Some(manager) = self.crash_manager.as_mut() {
            manager.begin_retirement();
        }
        let mut error = None;
        let observed_lifetime = self
            .observed_lifetime
            .take()
            .expect("active sandbox child should retain its observed lifetime");
        if let Err(observation_error) = observed_lifetime.stop() {
            error = Some(append_retirement_error(error, observation_error));
        }
        // Process-group cleanup remains an independent backstop for the narrow
        // interval between a fork and its observation. Run it even when tracked
        // retirement succeeds so a same-group process cannot survive merely
        // because its parent exited before the fork event was resolved.
        if self.separate_process_group
            && let Err(group_error) = platform::kill_process_group(self.child.id())
        {
            error = Some(append_retirement_error(
                error,
                format!(
                    "failed to stop `{}` process group: {group_error}",
                    platform::SANDBOX_EXEC
                ),
            ));
        }
        let manager_preparation = self
            .crash_manager
            .as_mut()
            .map(supervision::SandboxManager::prepare_finish);
        let mut direct_stop_failed = false;
        if error.is_some()
            && let Err(kill_error) = self.child.kill()
            && kill_error.raw_os_error() != Some(libc::ESRCH)
        {
            error = Some(append_retirement_error(
                error,
                format!(
                    "failed to stop direct `{}` process: {kill_error}",
                    platform::SANDBOX_EXEC
                ),
            ));
            direct_stop_failed = true;
        }

        let identity_released = if direct_stop_failed {
            false
        } else {
            match self.child.wait() {
                Ok(_) => true,
                Err(wait_error) => {
                    let identity_released = wait_error.raw_os_error() == Some(libc::ECHILD);
                    error = Some(append_retirement_error(
                        error,
                        format!(
                            "failed to reap stopped `{}`: {wait_error}",
                            platform::SANDBOX_EXEC
                        ),
                    ));
                    identity_released
                }
            }
        };
        let preserve_manager_directory = error.is_some()
            || manager_preparation.is_some_and(|preparation| {
                preparation != supervision::CleanupPreparation::Complete
            });
        if let Some(manager) = self.crash_manager.take()
            && let Err(manager_error) = manager.finish(preserve_manager_directory)
        {
            error = Some(append_retirement_error(error, manager_error));
        }
        if error.is_some() || manager_preparation == Some(supervision::CleanupPreparation::TimedOut)
        {
            self.preserve_temporary_directory();
        } else {
            self.remove_temporary_directory();
        }

        if direct_stop_failed {
            let error = error.expect("direct stop failure should retain its error");
            self.retirement = SandboxedChildRetirement::Failed {
                error: error.clone(),
            };
            return Err(error);
        }
        self.retirement = if identity_released {
            SandboxedChildRetirement::Retired {
                error: error.clone(),
            }
        } else {
            SandboxedChildRetirement::AwaitingReap {
                error: error.clone(),
            }
        };
        stored_retirement_result(&error)
    }

    fn reap_after_stop(&mut self, prior_error: Option<String>) -> Result<(), String> {
        match self.child.wait() {
            Ok(_) => {
                if prior_error.is_some() {
                    self.preserve_temporary_directory();
                }
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
                self.preserve_temporary_directory();
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

    fn preserve_temporary_directory(&mut self) {
        if let Some(directory) = self.temporary_directory.take() {
            std::mem::forget(directory);
        }
    }

    fn remove_temporary_directory(&mut self) {
        drop(self.temporary_directory.take());
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
