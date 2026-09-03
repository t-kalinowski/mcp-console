use super::job_control::{ForegroundTerminal, SignalRelay};
use super::manager::SandboxManager;
use super::root_exit_waiter::{RootExitWaiter, RootWait};
use crate::sandbox::{
    TARGET_GATE_RELEASE, child::terminate_standalone_root, file_descriptors, platform,
};
use std::io::{ErrorKind, Write as _};
use std::os::fd::AsRawFd as _;
use std::os::unix::net::UnixStream;
use std::process::{Child, Command, ExitCode, ExitStatus};
use std::time::Duration;

const ROOT_STOP_TIMEOUT: Duration = Duration::from_secs(1);

pub(in crate::sandbox) fn status(
    mut sandbox_command: Command,
    temporary_directory: platform::TemporaryDirectory,
    target_gate: UnixStream,
    mut launcher_gate: UnixStream,
) -> Result<ExitCode, String> {
    let mut manager = SandboxManager::spawn(Duration::from_secs(5))?;
    let signal_relay = SignalRelay::install()?;
    let mut foreground_terminal = ForegroundTerminal::detect()?;
    signal_relay.configure_child(&mut sandbox_command, foreground_terminal.descriptor());

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

    let mut root_waiter = match RootExitWaiter::start(child.id() as libc::pid_t, &signal_relay) {
        Ok(root_waiter) => root_waiter,
        Err(error) => {
            let mut error = match terminate_standalone_root(&mut child, ROOT_STOP_TIMEOUT) {
                Ok(_) => error,
                Err(kill_error) => additional_error(error, kill_error),
            };
            if let Err(terminal_error) = foreground_terminal.restore() {
                error = additional_error(error, terminal_error);
            }
            drop(manager);
            if let Err(signal_error) = signal_relay.drain_pending_and_restore() {
                error = additional_error(error, signal_error);
            }
            preserve(temporary_directory);
            return Err(error);
        }
    };

    if let Err(error) = manager.observe(child.id(), temporary_directory.path()) {
        preserve(temporary_directory);
        let root_result = terminate_standalone_root(&mut child, ROOT_STOP_TIMEOUT);
        let mut error = match root_result {
            Ok(_) => error,
            Err(kill_error) => additional_error(error, kill_error),
        };
        if let Err(terminal_error) = foreground_terminal.restore() {
            error = additional_error(error, terminal_error);
        }
        if let Err(manager_error) = manager.retire() {
            error = additional_error(error, manager_error);
        }
        if let Err(signal_error) = signal_relay.drain_pending_and_restore() {
            error = additional_error(error, signal_error);
        }
        return Err(error);
    }

    manager.monitor_for_standalone(child.id(), temporary_directory, root_waiter.wakeup());
    if let Err(write_error) = launcher_gate.write_all(&[TARGET_GATE_RELEASE])
        && write_error.kind() != ErrorKind::BrokenPipe
    {
        let mut error = format!("failed to release sandbox target startup gate: {write_error}");
        if let Err(stop_error) = stop_managed_root(&mut child, manager) {
            error = additional_error(error, stop_error);
        }
        if let Err(owner_error) = restore_launcher_state(&mut foreground_terminal, signal_relay) {
            error = additional_error(error, owner_error);
        }
        return Err(error);
    }
    drop(launcher_gate);

    let root_wait = match wait_for_root_exit(&child, &signal_relay, &mut root_waiter) {
        Ok(root_wait) => root_wait,
        Err(mut error) => {
            if let Err(stop_error) = stop_managed_root(&mut child, manager) {
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
            &mut child,
            manager,
            &mut foreground_terminal,
            signal_relay,
        );
    }
    let owner_result = restore_launcher_state(&mut foreground_terminal, signal_relay);
    let manager_result = manager.retire();

    let status = match child.wait() {
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
    manager: SandboxManager,
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

fn stop_managed_root(child: &mut Child, manager: SandboxManager) -> Result<(), String> {
    stop_managed_root_with_status(child, manager).map(|_| ())
}

fn stop_managed_root_with_status(
    child: &mut Child,
    manager: SandboxManager,
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

fn preserve(temporary_directory: platform::TemporaryDirectory) {
    // A live descendant may still be using this path. Deliberately leak the
    // guard on lifetime-cleanup failure instead of deleting files underneath it.
    temporary_directory.preserve();
}
