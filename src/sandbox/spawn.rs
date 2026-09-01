use std::os::unix::net::UnixStream;
use std::process::{Child, ExitCode};

use super::child::append_retirement_error;
use super::command::{SandboxedChild, SandboxedChildRetirement, SandboxedCommand};
use super::{CRASH_MANAGER_CLEANUP_TIMEOUT, platform, supervision};

impl SandboxedCommand {
    /// Spawns a hidden gated root, starts host-side descendant observation,
    /// commits an independent host crash manager, releases the configured relay,
    /// and transfers the private temporary-directory guard to the returned child.
    ///
    /// Relay code cannot execute before both host observers report readiness.
    /// Darwin still cannot resolve every later fork atomically: a descendant
    /// that becomes orphaned before its fork event is resolved remains outside
    /// that observer's guarantee. Abrupt owner failure before manager readiness
    /// closes the private gate without executing relay code, but remains outside
    /// manager-owned temporary-directory cleanup.
    pub(crate) fn spawn(mut self) -> Result<SandboxedChild, String> {
        let mut startup_gate = self
            .startup_gate
            .take()
            .expect("sandboxed relay spawn should retain its startup gate");
        self.command.env("TMPDIR", self.temporary_directory.path());
        let mut child = self
            .command
            .spawn()
            .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;
        startup_gate.child_spawned();
        let observed_lifetime = match supervision::ObservedLifetime::start(child.id()) {
            Ok(lifetime) => lifetime,
            Err(error) => {
                let error =
                    stop_after_observation_failure(&mut child, self.separate_process_group, error);
                // Observation failed before ownership was established. Preserve
                // the directory because a process that escaped observation may
                // still be using it even after process-group fallback cleanup.
                std::mem::forget(self.temporary_directory);
                return Err(error);
            }
        };
        let crash_manager = match supervision::SandboxManager::start(
            child.id(),
            self.temporary_directory.path(),
            CRASH_MANAGER_CLEANUP_TIMEOUT,
        ) {
            Ok(manager) => manager,
            Err(error) => {
                let mut child = SandboxedChild {
                    child,
                    observed_lifetime: Some(observed_lifetime),
                    crash_manager: None,
                    retirement: SandboxedChildRetirement::Active,
                    separate_process_group: self.separate_process_group,
                    temporary_directory: Some(self.temporary_directory),
                };
                let cleanup = child.force_stop().err();
                return Err(cleanup.map_or(error.clone(), |cleanup| {
                    append_retirement_error(Some(error), cleanup)
                }));
            }
        };
        let mut child = SandboxedChild {
            child,
            observed_lifetime: Some(observed_lifetime),
            crash_manager: Some(crash_manager),
            retirement: SandboxedChildRetirement::Active,
            separate_process_group: self.separate_process_group,
            temporary_directory: Some(self.temporary_directory),
        };
        if let Err(error) = startup_gate.release() {
            let cleanup = child.force_stop().err();
            return Err(cleanup.map_or(error.clone(), |cleanup| {
                append_retirement_error(Some(error), cleanup)
            }));
        }
        Ok(child)
    }

    /// Runs a standalone command with launcher-owned normal retirement and an
    /// independent crash manager for abrupt launcher loss.
    ///
    /// A hidden wrapper blocks on a private release descriptor before executing
    /// the requested program. The launcher attaches its local observer, waits
    /// for the manager to adopt the temporary directory and report readiness,
    /// then releases that same root process into the program. A descendant that
    /// later becomes orphaned before either observer sees its fork remains
    /// outside that observer's guarantee. Abrupt launcher failure before manager
    /// readiness remains outside crash cleanup.
    pub(crate) fn status(
        mut self,
        target_gate: UnixStream,
        launcher_gate: UnixStream,
    ) -> Result<ExitCode, String> {
        debug_assert!(self.startup_gate.is_none());
        self.command.env("TMPDIR", self.temporary_directory.path());
        supervision::status(
            self.command,
            self.temporary_directory,
            target_gate,
            launcher_gate,
        )
    }
}

fn stop_after_observation_failure(
    child: &mut Child,
    separate_process_group: bool,
    mut error: String,
) -> String {
    if separate_process_group && let Err(group_error) = platform::kill_process_group(child.id()) {
        error = append_retirement_error(
            Some(error),
            format!(
                "failed to stop `{}` process group: {group_error}",
                platform::SANDBOX_EXEC
            ),
        );
    }
    supervision::stop_direct_child(child, error)
}
