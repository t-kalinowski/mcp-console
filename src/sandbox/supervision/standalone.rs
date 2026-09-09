use super::job_control::{ForegroundTerminal, SignalRelay};
use super::manager::SandboxManager;
use super::root_exit_waiter::{RootExitWaiter, RootWait};
use crate::process_descriptors;
use crate::sandbox::{
    child::{append_retirement_error, terminate_standalone_root, terminate_unmanaged_child},
    platform,
    runner::{self, Setup},
};
use std::ffi::{OsStr, OsString};
use std::process::{Child, Command, ExitCode, ExitStatus};
use std::time::Duration;

const ROOT_STOP_TIMEOUT: Duration = Duration::from_secs(1);

pub(in crate::sandbox) fn status(
    mut command: Command,
    temporary_directory: platform::TemporaryDirectory,
    program: &OsStr,
    arguments: &[OsString],
    owner: Option<super::SandboxOwner>,
) -> Result<ExitCode, String> {
    let ignored = runner::ignored_signals()
        .map_err(|error| format!("failed to read inherited signal dispositions: {error}"))?;
    let signal_relay = SignalRelay::install(owner.is_some())?;
    let mut setup = Setup::new(
        &mut command,
        &temporary_directory,
        program,
        arguments,
        signal_relay.target_signal_mask(),
        ignored,
    )?;
    let mut foreground_terminal = ForegroundTerminal::detect()?;
    let owned = owner.is_some();
    let (mut root, mut root_waiter) = match start_managed_root(
        command,
        temporary_directory,
        &signal_relay,
        foreground_terminal.descriptor(),
        owner,
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

    let root_wait = match (|| {
        root_waiter.watch_setup(setup.descriptor())?;
        wait_for_root_exit(
            &root.child,
            &signal_relay,
            &mut root_waiter,
            owned,
            &mut setup,
        )
    })() {
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
    if owned {
        return finish_owned_completion(
            &mut root.child,
            &mut root.supervisor,
            &mut foreground_terminal,
            signal_relay,
            root_wait == RootCompletion::RetirementRequested,
        );
    }
    let owner_result = restore_launcher_state(&mut foreground_terminal, signal_relay);
    let manager_result = root.supervisor.retire();

    let status = match root.child.wait() {
        Ok(status) => status,
        Err(wait_error) => {
            let mut error = format!(
                "failed to wait for `{}`: {wait_error}",
                platform::RUNNER_NAME
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

struct ManagedRoot {
    child: Child,
    supervisor: SandboxManager,
}

fn start_managed_root(
    mut command: Command,
    mut temporary_directory: platform::TemporaryDirectory,
    signal_relay: &SignalRelay,
    terminal_descriptor: Option<libc::c_int>,
    owner: Option<super::SandboxOwner>,
) -> Result<(ManagedRoot, RootExitWaiter), String> {
    command.env("TMPDIR", temporary_directory.path());
    signal_relay.configure_child(&mut command, terminal_descriptor);

    let mut child = command
        .spawn()
        .map_err(|error| format!("failed to launch `{}`: {error}", platform::RUNNER_NAME))?;
    drop(command);
    // The runner already inherited the original fd 0. Detach the owned
    // launcher's input before starting any host-side observer or manager.
    if owner.is_some()
        && terminal_descriptor != Some(libc::STDIN_FILENO)
        && let Err(error) = process_descriptors::detach_stdin()
    {
        return Err(fail_before_manager_start(child, temporary_directory, error));
    }
    let root_waiter = match RootExitWaiter::start(child.id() as libc::pid_t, signal_relay, owner) {
        Ok(root_waiter) => root_waiter,
        Err(error) => {
            return Err(fail_before_manager_start(child, temporary_directory, error));
        }
    };
    let supervisor = match SandboxManager::start(
        child.id(),
        &mut temporary_directory,
        crate::sandbox::MANAGER_CLEANUP_TIMEOUT,
        signal_relay,
        root_waiter.wakeup(),
    ) {
        Ok(supervisor) => supervisor,
        Err(error) => {
            return Err(fail_unmanaged_startup(child, temporary_directory, error));
        }
    };

    if let Err(error) = root_waiter.validate_owner() {
        return Err(finish_failed_startup(&mut child, supervisor, error));
    }
    Ok((ManagedRoot { child, supervisor }, root_waiter))
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
    mut supervisor: SandboxManager,
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
            format!("failed to reap `{}`: {wait_error}", platform::RUNNER_NAME),
        ),
    }
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum RootCompletion {
    RetirementRequested,
    RootExited,
    ManagerFinished,
}

fn wait_for_root_exit(
    child: &Child,
    signal_relay: &SignalRelay,
    root_waiter: &mut RootExitWaiter,
    retire_on_sigterm: bool,
    setup: &mut Setup,
) -> Result<RootCompletion, String> {
    loop {
        if root_has_exited(child, Duration::ZERO)? {
            return Ok(RootCompletion::RootExited);
        }

        let process_group = child.id() as libc::pid_t;
        if signal_relay.relay_pending(process_group, retire_on_sigterm)? {
            return Ok(RootCompletion::RetirementRequested);
        }
        setup
            .write_once()
            .map_err(|error| format!("failed to send sandbox setup: {error}"))?;
        match root_waiter.wait_for_events(None) {
            Ok(RootWait::RootExited) => return Ok(RootCompletion::RootExited),
            Ok(RootWait::OwnerExited) => return Ok(RootCompletion::RetirementRequested),
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
                    platform::RUNNER_NAME
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

fn finish_owned_completion(
    child: &mut Child,
    manager: &mut SandboxManager,
    foreground_terminal: &mut ForegroundTerminal,
    signal_relay: SignalRelay,
    retirement_requested: bool,
) -> Result<ExitCode, String> {
    let status_result = stop_managed_root_with_status(child, manager);
    let owner_result = restore_launcher_state(foreground_terminal, signal_relay);
    match (status_result, owner_result) {
        (Ok(_), Ok(())) if retirement_requested => Ok(ExitCode::SUCCESS),
        (Ok(status), Ok(())) => Ok(platform::exit_code(status)),
        (Err(error), Ok(())) | (Ok(_), Err(error)) => Err(error),
        (Err(error), Err(owner_error)) => Err(additional_error(error, owner_error)),
    }
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
            platform::RUNNER_NAME
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
                platform::RUNNER_NAME
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
