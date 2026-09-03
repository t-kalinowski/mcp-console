use super::job_control::{ForegroundTerminal, SignalRelay};
use super::manager::SandboxManager;
use super::root_exit_waiter::{RootExitWaiter, RootWait};
use crate::sandbox::{
    child::terminate_standalone_root,
    command::{ManagedRoot, SandboxedCommand},
    platform,
    spawn::StartOptions,
};
use std::process::{Child, ExitCode, ExitStatus};
use std::time::Duration;

const ROOT_STOP_TIMEOUT: Duration = Duration::from_secs(1);

pub(in crate::sandbox) fn status(sandboxed: SandboxedCommand) -> Result<ExitCode, String> {
    let signal_relay = SignalRelay::install()?;
    let mut foreground_terminal = ForegroundTerminal::detect()?;
    let (mut root, root_waiter) = match ManagedRoot::start(
        sandboxed,
        StartOptions {
            signal_relay: Some(&signal_relay),
            terminal_descriptor: foreground_terminal.descriptor(),
            cleanup_timeout: Duration::from_secs(5),
        },
    ) {
        Ok(started) => started,
        Err(error) => {
            let error = match restore_launcher_state(&mut foreground_terminal, signal_relay) {
                Ok(()) => error,
                Err(owner_error) => additional_error(error, owner_error),
            };
            return Err(error);
        }
    };
    let mut root_waiter = root_waiter.expect("standalone root should retain its exit waiter");

    let root_wait = match wait_for_root_exit(&root.child, &signal_relay, &mut root_waiter) {
        Ok(root_wait) => root_wait,
        Err(mut error) => {
            if let Err(stop_error) = stop_managed_root(&mut root.child, &mut root.supervisor) {
                error = additional_error(error, stop_error);
            }
            if let Err(owner_error) = restore_launcher_state(&mut foreground_terminal, signal_relay)
            {
                error = additional_error(error, owner_error);
            }
            return Err(error);
        }
    };
    // Keep the exited root waitable until host-side sandbox-lifetime cleanup has
    // completed. Root exit, launcher signals, and manager-monitor completion all
    // wake the same blocking wait.
    drop(root_waiter);
    if root_wait == RootCompletion::ManagerFinished {
        return finish_after_manager_exit(
            &mut root.child,
            &mut root.supervisor,
            &mut foreground_terminal,
            signal_relay,
        );
    }
    let owner_result = restore_launcher_state(&mut foreground_terminal, signal_relay);
    let manager_result = root.supervisor.retire();

    let status = match root.child.wait() {
        Ok(status) => status,
        Err(wait_error) => {
            let mut error = format!(
                "failed to wait for `{}`: {wait_error}",
                platform::SANDBOX_EXEC
            );
            if let Err(owner_error) = owner_result {
                error = additional_error(error, owner_error);
            }
            if let Err(manager_error) = manager_result {
                error = additional_error(error, manager_error);
            }
            return Err(error);
        }
    };
    match (owner_result, manager_result) {
        (Ok(()), Ok(())) => Ok(platform::exit_code(status)),
        (Err(error), Ok(())) | (Ok(()), Err(error)) => Err(error),
        (Err(error), Err(manager_error)) => Err(additional_error(error, manager_error)),
    }
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum RootCompletion {
    RootExited,
    ManagerFinished,
}

fn wait_for_root_exit(
    child: &Child,
    signal_relay: &SignalRelay,
    root_waiter: &mut RootExitWaiter,
) -> Result<RootCompletion, String> {
    loop {
        if root_has_exited(child, Duration::ZERO)? {
            return Ok(RootCompletion::RootExited);
        }

        let process_group = child.id() as libc::pid_t;
        signal_relay.relay_pending(process_group)?;
        match root_waiter.wait_for_events(None) {
            Ok(RootWait::RootExited) => return Ok(RootCompletion::RootExited),
            Ok(RootWait::Wakeup) => {
                return if root_has_exited(child, Duration::ZERO)? {
                    Ok(RootCompletion::RootExited)
                } else {
                    Ok(RootCompletion::ManagerFinished)
                };
            }
            Ok(RootWait::Events | RootWait::TimedOut) => {}
            Err(error) => return Err(error),
        }
    }
}

fn finish_after_manager_exit(
    child: &mut Child,
    manager: &mut SandboxManager,
    foreground_terminal: &mut ForegroundTerminal,
    signal_relay: SignalRelay,
) -> Result<ExitCode, String> {
    let manager_result = manager.retire();
    let root_exit_result = root_has_exited(child, ROOT_STOP_TIMEOUT);
    let status_result = match root_exit_result {
        Ok(true) => match child.wait() {
            Ok(status) => manager_result.map(|()| status),
            Err(wait_error) => {
                let wait_error = format!(
                    "failed to wait for terminated {}: {wait_error}",
                    platform::SANDBOX_EXEC
                );
                Err(match manager_result {
                    Ok(()) => wait_error,
                    Err(error) => additional_error(error, wait_error),
                })
            }
        },
        Ok(false) => {
            let mut error = manager_result.err().unwrap_or_else(|| {
                "sandbox manager recovery did not terminate the sandbox root".to_string()
            });
            if let Err(kill_error) = terminate_standalone_root(child, ROOT_STOP_TIMEOUT) {
                error = additional_error(error, kill_error);
            }
            Err(error)
        }
        Err(wait_error) => {
            let mut error = match manager_result {
                Ok(()) => wait_error,
                Err(error) => additional_error(error, wait_error),
            };
            if let Err(kill_error) = terminate_standalone_root(child, ROOT_STOP_TIMEOUT) {
                error = additional_error(error, kill_error);
            }
            Err(error)
        }
    };
    let owner_result = restore_launcher_state(foreground_terminal, signal_relay);

    let status = match status_result {
        Ok(status) => status,
        Err(mut error) => {
            if let Err(owner_error) = owner_result {
                error = additional_error(error, owner_error);
            }
            return Err(error);
        }
    };
    owner_result.map(|()| platform::exit_code(status))
}

fn restore_launcher_state(
    foreground_terminal: &mut ForegroundTerminal,
    signal_relay: SignalRelay,
) -> Result<(), String> {
    let terminal_result = foreground_terminal.restore();
    let signal_result = signal_relay.drain_pending_and_restore();
    match (terminal_result, signal_result) {
        (Ok(()), Ok(())) => Ok(()),
        (Err(error), Ok(())) | (Ok(()), Err(error)) => Err(error),
        (Err(error), Err(signal_error)) => Err(additional_error(error, signal_error)),
    }
}

fn root_has_exited(child: &Child, timeout: Duration) -> Result<bool, String> {
    platform::wait_for_process_exit_without_reaping(child.id(), timeout).map_err(|error| {
        format!(
            "failed to inspect `{}` exit status: {error}",
            platform::SANDBOX_EXEC
        )
    })
}

fn additional_error(primary: String, additional: String) -> String {
    format!("{primary}; additionally, {additional}")
}

fn stop_managed_root(child: &mut Child, manager: &mut SandboxManager) -> Result<(), String> {
    stop_managed_root_with_status(child, manager).map(|_| ())
}

fn stop_managed_root_with_status(
    child: &mut Child,
    manager: &mut SandboxManager,
) -> Result<ExitStatus, String> {
    match manager.retire() {
        Ok(()) => child.wait().map_err(|wait_error| {
            format!(
                "failed to wait for terminated {}: {wait_error}",
                platform::SANDBOX_EXEC
            )
        }),
        Err(mut error) => {
            if let Err(kill_error) = terminate_standalone_root(child, ROOT_STOP_TIMEOUT) {
                error = additional_error(error, kill_error);
            }
            Err(error)
        }
    }
}
