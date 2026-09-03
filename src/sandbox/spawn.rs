use std::io::ErrorKind;
use std::os::unix::process::CommandExt as _;
use std::process::{Child, ExitCode};
use std::time::Duration;

use super::child::{append_retirement_error, terminate_unmanaged_child};
use super::command::{ManagedRoot, SandboxedChild, SandboxedChildRetirement, SandboxedCommand};
use super::{file_descriptors, platform, supervision};

pub(super) struct StartOptions<'a> {
    pub(super) signal_relay: Option<&'a supervision::SignalRelay>,
    pub(super) terminal_descriptor: Option<libc::c_int>,
    pub(super) cleanup_timeout: Duration,
    pub(super) owner: Option<supervision::SandboxOwner>,
}

impl ManagedRoot {
    pub(super) fn start(
        sandboxed: SandboxedCommand,
        options: StartOptions<'_>,
    ) -> Result<(Self, Option<supervision::RootExitWaiter>), String> {
        let SandboxedCommand {
            mut command,
            mut temporary_directory,
            mut startup_gate,
        } = sandboxed;
        let gate_descriptor = startup_gate.inherited_descriptor();
        command.env("TMPDIR", temporary_directory.path());
        if let Some(signal_relay) = options.signal_relay {
            signal_relay.configure_child(&mut command, options.terminal_descriptor);
            file_descriptors::close_unlisted_except(&mut command, gate_descriptor)?;
        } else {
            command.process_group(0);
            file_descriptors::close_unlisted_from_multithreaded_parent_except(
                &mut command,
                vec![gate_descriptor],
            )?;
        }

        let mut child = command
            .spawn()
            .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;
        startup_gate.child_spawned();
        let root_waiter = match options.signal_relay {
            Some(signal_relay) => {
                match supervision::RootExitWaiter::start(
                    child.id() as libc::pid_t,
                    signal_relay,
                    options.owner,
                ) {
                    Ok(root_waiter) => Some(root_waiter),
                    Err(error) => {
                        return Err(fail_before_manager_start(child, temporary_directory, error));
                    }
                }
            }
            None => None,
        };
        let supervisor = match supervision::SandboxManager::start(
            child.id(),
            &mut temporary_directory,
            options.cleanup_timeout,
            options.signal_relay,
            root_waiter
                .as_ref()
                .map(supervision::RootExitWaiter::wakeup),
        ) {
            Ok(supervisor) => supervisor,
            Err(error) => {
                return Err(fail_unmanaged_startup(child, temporary_directory, error));
            }
        };

        if let Some(root_waiter) = root_waiter.as_ref()
            && let Err(error) = root_waiter.validate_owner()
        {
            return Err(finish_failed_startup(&mut child, supervisor, error));
        }

        if let Err(write_error) = startup_gate.release()
            && !(options.signal_relay.is_some() && write_error.kind() == ErrorKind::BrokenPipe)
        {
            let error = format!("failed to release sandbox target startup gate: {write_error}");
            return Err(finish_failed_startup(&mut child, supervisor, error));
        }

        Ok((Self { child, supervisor }, root_waiter))
    }
}

impl SandboxedCommand {
    /// Spawns a hidden gated root under one host-side lifetime manager.
    ///
    /// The root starts behind its private gate. The manager then adopts
    /// descendant observation and the private directory and installs
    /// manager-failure recovery before the root is released into either the
    /// built-in or a configured relay.
    pub(crate) fn spawn(self) -> Result<SandboxedChild, String> {
        let (root, root_waiter) = ManagedRoot::start(
            self,
            StartOptions {
                signal_relay: None,
                terminal_descriptor: None,
                cleanup_timeout: super::MANAGER_CLEANUP_TIMEOUT,
                owner: None,
            },
        )?;
        debug_assert!(root_waiter.is_none());

        Ok(SandboxedChild {
            root,
            retirement: SandboxedChildRetirement::Managed,
        })
    }

    /// Runs a standalone command with launcher-owned job control and the same
    /// primary host-side lifetime manager used for worker relays.
    pub(super) fn status(
        self,
        owner: Option<supervision::SandboxOwner>,
    ) -> Result<ExitCode, String> {
        supervision::status(self, owner)
    }
}

fn fail_before_manager_start(
    mut child: Child,
    temporary_directory: platform::TemporaryDirectory,
    error: String,
) -> String {
    let cleanup = terminate_unmanaged_child(&mut child, error);
    if !cleanup.identity_released {
        temporary_directory.preserve();
    }
    cleanup.error
}

fn fail_unmanaged_startup(
    mut child: Child,
    temporary_directory: platform::TemporaryDirectory,
    error: String,
) -> String {
    temporary_directory.preserve();
    terminate_unmanaged_child(&mut child, error).error
}

fn finish_failed_startup(
    child: &mut Child,
    mut supervisor: supervision::SandboxManager,
    error: String,
) -> String {
    if let Err(supervisor_error) = supervisor.retire() {
        let error = append_retirement_error(error, supervisor_error);
        return terminate_unmanaged_child(child, error).error;
    }
    match child.wait() {
        Ok(_) => error,
        Err(wait_error) => append_retirement_error(
            error,
            format!("failed to reap `{}`: {wait_error}", platform::SANDBOX_EXEC),
        ),
    }
}
