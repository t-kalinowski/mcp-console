use super::job_control::{ForegroundTerminal, SignalRelay};
use super::root_exit_waiter::{RootExitWaiter, RootWait};
use super::{
    CleanupPreparation, ObservedLifetime, SandboxManager, additional_error, preserve,
    stop_direct_child,
};
use crate::sandbox::{
    CRASH_MANAGER_CLEANUP_TIMEOUT, TARGET_GATE_RELEASE, file_descriptors, platform,
};
use std::io::Write as _;
use std::os::fd::AsRawFd as _;
use std::os::unix::net::UnixStream;
use std::process::{Child, Command, ExitCode};
use std::time::Duration;

pub(super) fn status(
    mut sandbox_command: Command,
    temporary_directory: platform::TemporaryDirectory,
    target_gate: UnixStream,
    mut launcher_gate: UnixStream,
) -> Result<ExitCode, String> {
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
            let mut error = stop_direct_child(&mut child, error);
            if let Err(terminal_error) = foreground_terminal.restore() {
                error = additional_error(error, terminal_error);
            }
            preserve(temporary_directory);
            return Err(error);
        }
    };
    let observed_lifetime =
        match ObservedLifetime::start_for_standalone(child.id(), root_waiter.wakeup()) {
            Ok(observer) => observer,
            Err(error) => {
                let mut error = stop_direct_child(&mut child, error);
                if let Err(terminal_error) = foreground_terminal.restore() {
                    error = additional_error(error, terminal_error);
                }
                preserve(temporary_directory);
                return Err(error);
            }
        };
    let mut manager = match SandboxManager::start_for_standalone(
        child.id(),
        temporary_directory.path(),
        CRASH_MANAGER_CLEANUP_TIMEOUT,
        root_waiter.wakeup(),
    ) {
        Ok(manager) => manager,
        Err(error) => {
            let mut error =
                retire_after_manager_start_failure(observed_lifetime, &mut child, error);
            if let Err(terminal_error) = foreground_terminal.restore() {
                error = additional_error(error, terminal_error);
            }
            preserve(temporary_directory);
            return Err(error);
        }
    };

    if let Err(write_error) = launcher_gate.write_all(&[TARGET_GATE_RELEASE]) {
        drop(launcher_gate);
        let mut error = format!("failed to release sandbox target startup gate: {write_error}");
        let _ = manager.begin_retirement();
        if let Err(retirement_error) = observed_lifetime.stop() {
            error = additional_error(error, retirement_error);
        }
        error = stop_direct_child(&mut child, error);
        if let Err(terminal_error) = foreground_terminal.restore() {
            error = additional_error(error, terminal_error);
        }
        let _ = manager.prepare_finish();
        if let Err(manager_error) = manager.finish(true) {
            error = additional_error(error, manager_error);
        }
        preserve(temporary_directory);
        return Err(error);
    }
    drop(launcher_gate);

    let wait_error = wait_for_root_exit(&child, &signal_relay, &mut root_waiter).err();
    // Keep the exited root waitable until host-side sandbox-lifetime cleanup has
    // completed. Root exit, launcher signals, and owner-side supervision failure
    // all wake the same blocking wait.
    drop(root_waiter);
    let _ = manager.begin_retirement();

    let retirement_error = observed_lifetime.stop().err();
    let terminal_error = foreground_terminal.restore().err();
    let manager_preparation = manager.prepare_finish();

    let mut error = wait_error;
    if let Some(retirement_error) = retirement_error {
        error = Some(match error {
            Some(error) => additional_error(error, retirement_error),
            None => retirement_error,
        });
    }
    if let Some(terminal_error) = terminal_error {
        error = Some(match error {
            Some(error) => additional_error(error, terminal_error),
            None => terminal_error,
        });
    }

    let status = if let Some(cleanup_error) = error.take() {
        error = Some(stop_direct_child(&mut child, cleanup_error));
        None
    } else {
        match child.wait() {
            Ok(status) => Some(status),
            Err(wait_error) => {
                error = Some(format!(
                    "failed to wait for `{}`: {wait_error}",
                    platform::SANDBOX_EXEC
                ));
                None
            }
        }
    };

    let preserve_manager_directory =
        error.is_some() || manager_preparation != CleanupPreparation::Complete;
    if let Err(manager_error) = manager.finish(preserve_manager_directory) {
        error = Some(match error {
            Some(error) => additional_error(error, manager_error),
            None => manager_error,
        });
    }
    if let Some(error) = error {
        preserve(temporary_directory);
        return Err(error);
    }

    if manager_preparation == CleanupPreparation::TimedOut {
        preserve(temporary_directory);
    } else {
        drop(temporary_directory);
    }
    Ok(platform::exit_code(status.expect(
        "successful standalone retirement should retain the root status",
    )))
}

fn wait_for_root_exit(
    child: &Child,
    signal_relay: &SignalRelay,
    root_waiter: &mut RootExitWaiter,
) -> Result<(), String> {
    loop {
        if platform::wait_for_process_exit_without_reaping(child.id(), Duration::ZERO).map_err(
            |error| {
                format!(
                    "failed to inspect `{}` exit status: {error}",
                    platform::SANDBOX_EXEC
                )
            },
        )? {
            return Ok(());
        }

        let process_group = child.id() as libc::pid_t;
        signal_relay.relay_pending(process_group)?;
        match root_waiter.wait_for_events() {
            Ok(RootWait::RootExited | RootWait::Wakeup) => return Ok(()),
            Ok(RootWait::Events) => {}
            Err(error) => return Err(error),
        }
    }
}

fn retire_after_manager_start_failure(
    observed_lifetime: ObservedLifetime,
    child: &mut Child,
    error: String,
) -> String {
    let error = match observed_lifetime.stop() {
        Ok(()) => error,
        Err(retirement_error) => additional_error(error, retirement_error),
    };
    stop_direct_child(child, error)
}
