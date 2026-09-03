use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt as _;
use std::process::{Child, ExitCode};

use super::child::{append_retirement_error, terminate_unmanaged_child};
use super::command::{SandboxedChild, SandboxedChildRetirement, SandboxedCommand};
use super::{MANAGER_CLEANUP_TIMEOUT, platform, supervision};

impl SandboxedCommand {
    /// Spawns a hidden gated root under one host-side lifetime manager.
    ///
    /// The manager starts before the root, adopts descendant observation and
    /// the private directory, and installs manager-failure recovery before the
    /// root is released into either the built-in or a configured relay.
    pub(crate) fn spawn(mut self) -> Result<SandboxedChild, String> {
        let mut startup_gate = self
            .startup_gate
            .take()
            .expect("sandboxed relay spawn should retain its startup gate");
        self.command
            .env("TMPDIR", self.temporary_directory.path())
            .process_group(0);
        let mut manager = supervision::SandboxManager::spawn(MANAGER_CLEANUP_TIMEOUT)?;
        let mut child = self
            .command
            .spawn()
            .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;
        startup_gate.child_spawned();

        if let Err(error) = manager.observe(child.id(), self.temporary_directory.path()) {
            self.temporary_directory.preserve();
            drop(startup_gate);
            drop(manager);
            return Err(terminate_unmanaged_child(&mut child, error).error);
        }
        manager.monitor(child.id(), self.temporary_directory);
        if let Err(error) = manager.commit() {
            let manager_error = manager.retire().err();
            drop(startup_gate);
            return Err(stop_after_manager_failure(&mut child, error, manager_error));
        }
        if let Err(error) = startup_gate.release() {
            let manager_error = manager.retire().err();
            return Err(stop_after_manager_failure(&mut child, error, manager_error));
        }

        Ok(SandboxedChild {
            child,
            manager: Some(manager),
            retirement: SandboxedChildRetirement::Managed,
        })
    }

    /// Runs a standalone command with launcher-owned job control and the same
    /// primary host-side lifetime manager used for worker relays.
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

fn stop_after_manager_failure(
    child: &mut Child,
    mut error: String,
    manager_error: Option<String>,
) -> String {
    let Some(manager_error) = manager_error else {
        if let Err(wait_error) = child.wait() {
            error = append_retirement_error(
                error,
                format!("failed to reap `{}`: {wait_error}", platform::SANDBOX_EXEC),
            );
        }
        return error;
    };
    error = append_retirement_error(error, manager_error);
    terminate_unmanaged_child(child, error).error
}
