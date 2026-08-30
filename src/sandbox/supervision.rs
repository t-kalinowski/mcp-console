#[path = "supervision/job_control.rs"]
mod job_control;
#[path = "supervision/manager.rs"]
mod manager;
#[path = "supervision/process.rs"]
mod process;
#[path = "supervision/process_tracker.rs"]
mod process_tracker;

use self::job_control::{ForegroundTerminal, SignalRelay};
pub(crate) use self::manager::SandboxManager;
use self::process_tracker::{EventWait, RootExitWaiter};
use super::file_descriptors::configure as configure_file_descriptors;
use super::platform;
use std::os::fd::RawFd;
use std::process::{Child, Command, ExitCode, ExitStatus};
use std::time::Duration;

pub(super) fn configure_command(
    command: &mut Command,
    inherited_descriptors: Vec<RawFd>,
) -> Result<(), String> {
    configure_file_descriptors(command, inherited_descriptors)
}

pub(super) fn run_manager() -> Result<(), String> {
    manager::run()
}

pub(super) fn status(
    mut sandbox_command: Command,
    temporary_directory: platform::TemporaryDirectory,
) -> Result<ExitCode, String> {
    let mut manager = SandboxManager::spawn(Duration::from_secs(5))?;
    let signal_relay = SignalRelay::install()?;
    let mut foreground_terminal = ForegroundTerminal::detect();
    signal_relay.configure_child(&mut sandbox_command, foreground_terminal.descriptor());
    // The standalone launcher is single-threaded. Take the descriptor snapshot
    // after opening manager and signal state so none of it reaches the target.
    super::file_descriptors::close_unlisted(&mut sandbox_command)?;

    let mut child = sandbox_command
        .spawn()
        .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;
    let mut root_waiter = match RootExitWaiter::start(child.id() as libc::pid_t, &signal_relay) {
        Ok(root_waiter) => root_waiter,
        Err(error) => {
            let error = match kill_root(&mut child) {
                Ok(_) => error,
                Err(kill_error) => additional_error(error, kill_error),
            };
            let error = match foreground_terminal.restore() {
                Ok(()) => error,
                Err(terminal_error) => additional_error(error, terminal_error),
            };
            preserve(temporary_directory);
            return Err(error);
        }
    };

    if let Err(error) = manager.observe(child.id(), temporary_directory.path()) {
        preserve(temporary_directory);
        let root_result = kill_root(&mut child);
        let mut error = match root_result {
            Ok(_) => error,
            Err(kill_error) => additional_error(error, kill_error),
        };
        if let Err(terminal_error) = foreground_terminal.restore() {
            error = additional_error(error, terminal_error);
        }
        if let Err(manager_error) = manager.finish() {
            error = additional_error(error, manager_error);
        }
        return Err(error);
    }

    if let Err(mut error) = manager.commit() {
        // A failed acknowledgement is ambiguous: the manager may already have
        // accepted ownership. Ask it to stop the lifetime before reaping the
        // root, and use process-group cleanup only if that request fails.
        match manager.stop() {
            Ok(()) => {
                if let Err(wait_error) = child.wait() {
                    error = additional_error(
                        error,
                        format!(
                            "failed to wait for terminated {}: {wait_error}",
                            platform::SANDBOX_EXEC
                        ),
                    );
                }
            }
            Err(mut stop_error) => {
                preserve(temporary_directory);
                if let Err(kill_error) = kill_root(&mut child) {
                    stop_error = additional_error(stop_error, kill_error);
                }
                error = additional_error(error, stop_error);
            }
        }
        if let Err(terminal_error) = foreground_terminal.restore() {
            error = additional_error(error, terminal_error);
        }
        return Err(error);
    }
    manager.monitor(child.id(), temporary_directory);

    if let Err(mut error) = wait_for_root_exit(&child, &signal_relay, &mut root_waiter) {
        if let Err(stop_error) = stop_managed_root(&mut child, manager) {
            error = additional_error(error, stop_error);
        }
        if let Err(terminal_error) = foreground_terminal.restore() {
            error = additional_error(error, terminal_error);
        }
        return Err(error);
    }
    let terminal_result = foreground_terminal.restore();

    // Keep the exited root waitable until host-side sandbox-lifetime cleanup has
    // completed. The local observer only supplies root-exit and
    // job-control wakeups.
    drop(root_waiter);
    let manager_result = manager.finish();

    let status = match child.wait() {
        Ok(status) => status,
        Err(wait_error) => {
            let mut error = format!(
                "failed to wait for `{}`: {wait_error}",
                platform::SANDBOX_EXEC
            );
            if let Err(terminal_error) = terminal_result {
                error = additional_error(error, terminal_error);
            }
            if let Err(manager_error) = manager_result {
                error = additional_error(error, manager_error);
            }
            return Err(error);
        }
    };
    match (terminal_result, manager_result) {
        (Ok(()), Ok(())) => Ok(platform::exit_code(status)),
        (Err(error), Ok(())) | (Ok(()), Err(error)) => Err(error),
        (Err(error), Err(manager_error)) => Err(additional_error(error, manager_error)),
    }
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
        match root_waiter.wait_for_events(None) {
            Ok(EventWait::RootExited) => return Ok(()),
            Ok(EventWait::Events | EventWait::TimedOut) => {}
            Err(error) => return Err(error),
        }
    }
}

fn additional_error(primary: String, additional: String) -> String {
    format!("{primary}; additionally, {additional}")
}

fn stop_managed_root(child: &mut Child, manager: SandboxManager) -> Result<(), String> {
    match manager.stop() {
        Ok(()) => child.wait().map(|_| ()).map_err(|wait_error| {
            format!(
                "failed to wait for terminated {}: {wait_error}",
                platform::SANDBOX_EXEC
            )
        }),
        Err(mut error) => {
            if let Err(kill_error) = kill_root(child) {
                error = additional_error(error, kill_error);
            }
            Err(error)
        }
    }
}

// Callers retain the direct child waitably until after this function signals
// its process group, so its PID and process-group ID cannot be reused.
fn kill_root(child: &mut Child) -> Result<ExitStatus, String> {
    let group_result = unsafe { libc::kill(-(child.id() as libc::pid_t), libc::SIGKILL) };
    let group_error = (group_result != 0)
        .then(std::io::Error::last_os_error)
        .filter(|error| error.raw_os_error() != Some(libc::ESRCH));

    match child.try_wait() {
        Ok(Some(status)) => return Ok(status),
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
        let group_error = group_error
            .map(|group_error| format!("; process-group termination also failed: {group_error}"))
            .unwrap_or_default();
        return Err(format!(
            "failed to terminate direct {} process: {error}{group_error}",
            platform::SANDBOX_EXEC
        ));
    }

    child.wait().map_err(|error| {
        let group_error = group_error
            .map(|group_error| format!("; process-group termination also failed: {group_error}"))
            .unwrap_or_default();
        format!(
            "failed to wait for terminated {}: {error}{group_error}",
            platform::SANDBOX_EXEC
        )
    })
}

fn preserve(temporary_directory: platform::TemporaryDirectory) {
    // A live descendant may still be using this path. Deliberately leak the
    // guard on lifetime-cleanup failure instead of deleting files underneath it.
    temporary_directory.preserve();
}
