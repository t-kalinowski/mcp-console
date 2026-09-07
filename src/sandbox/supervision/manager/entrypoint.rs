use super::super::process::{ProcessIdentity, process_info, signal_process};
use super::super::process_tracker::{DescendantTracker, EventWait};
use super::{READY, stop_process_group, with_prior_error};
use crate::sandbox::platform;
use std::io::{ErrorKind, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::time::Duration;

pub(super) fn run(
    root_pid: u32,
    cleanup_timeout_millis: u64,
    temporary_directory: PathBuf,
) -> Result<(), String> {
    // SAFETY: the owner transfers its private control socket as the manager's
    // standard input and retains no manager-side copy after spawning.
    let stream = unsafe { UnixStream::from_raw_fd(libc::STDIN_FILENO) };

    // SAFETY: getppid(2) has no pointer or lifetime preconditions.
    let owner_pid = unsafe { libc::getppid() };
    if owner_pid <= 0 {
        return Err("sandbox manager owner PID is invalid".to_string());
    }
    let root_pid = libc::pid_t::try_from(root_pid)
        .ok()
        .filter(|pid| *pid > 0)
        .ok_or_else(|| "sandbox manager received an invalid root PID".to_string())?;
    if cleanup_timeout_millis == 0 {
        return Err("sandbox manager cleanup timeout is invalid".to_string());
    }
    let cleanup_timeout = Duration::from_millis(cleanup_timeout_millis);
    let info = process_info(root_pid)?
        .ok_or_else(|| format!("sandbox root {root_pid} exited before manager startup"))?;
    if info.parent_pid != owner_pid {
        return Err(format!(
            "sandbox root {root_pid} is not a child of manager owner {owner_pid}"
        ));
    }

    let tracker =
        DescendantTracker::start(root_pid).map_err(|failure| failure.retire(cleanup_timeout))?;
    let temporary_directory = platform::TemporaryDirectory::adopt(temporary_directory, owner_pid)?;
    let mut state = ManagerState {
        stream,
        tracker,
        root: info.identity,
        temporary_directory,
        cleanup_timeout,
    };

    let cause = if let Err(error) = state.tracker.watch_control(state.stream.as_raw_fd()) {
        ExitCause::StartupFailed(error)
    } else if let Err(error) = state.stream.write_all(&[READY]) {
        ExitCause::StartupFailed(format!(
            "failed to report sandbox manager readiness: {error}"
        ))
    } else {
        observe_lifetime(&mut state).unwrap_or_else(ExitCause::ObservationFailed)
    };
    retire(cause, state)
}

struct ManagerState {
    stream: UnixStream,
    tracker: DescendantTracker,
    root: ProcessIdentity,
    temporary_directory: platform::TemporaryDirectory,
    cleanup_timeout: Duration,
}

enum ExitCause {
    StartupFailed(String),
    OwnerLost(Option<String>),
    RootExited(Option<String>),
    ObservationFailed(String),
}

fn observe_lifetime(state: &mut ManagerState) -> Result<ExitCause, String> {
    loop {
        if state.tracker.root_has_exited()? {
            return Ok(ExitCause::RootExited(None));
        }
        match state.tracker.wait_for_events(None)? {
            EventWait::Events(events) => {
                let control_error = if events.control_readable {
                    read_owner_control(&mut state.stream).err()
                } else {
                    None
                };
                if events.root_exited
                    || events.control_readable && state.tracker.root_has_exited()?
                {
                    return Ok(ExitCause::RootExited(control_error));
                }
                if events.control_readable {
                    return Ok(ExitCause::OwnerLost(control_error));
                }
            }
            EventWait::TimedOut => {}
        }
    }
}

fn read_owner_control(stream: &mut UnixStream) -> Result<(), String> {
    let mut control = [0];
    loop {
        match stream.read(&mut control) {
            Ok(0) => return Ok(()),
            Ok(_) => {
                return Err("sandbox manager received data after readiness".to_string());
            }
            Err(error) if error.kind() == ErrorKind::Interrupted => {}
            Err(error) => return Err(format!("sandbox manager control failed: {error}")),
        }
    }
}

fn retire(cause: ExitCause, mut state: ManagerState) -> Result<(), String> {
    state.tracker.remove_control_watch();
    let (startup_failed, root_exited, observation_failed, mut error) = match cause {
        ExitCause::StartupFailed(error) => (true, false, false, Some(error)),
        ExitCause::OwnerLost(error) => (false, false, false, error),
        ExitCause::RootExited(error) => (false, true, false, error),
        ExitCause::ObservationFailed(error) => (false, false, true, Some(error)),
    };
    let mut cleanup_failed = false;

    let root_signal_failed = if root_exited || observation_failed {
        false
    } else {
        cleanup_failed |= record_error(&mut error, stop_process_group(state.root));
        let root_signal = signal_process(state.root, libc::SIGKILL).map(|_| ());
        let root_signal_failed = record_error(&mut error, root_signal);
        cleanup_failed |= root_signal_failed;
        root_signal_failed
    };

    // An observation failure can leave a fork event unprocessed. Snapshot and
    // stop the tracked tree before signaling the root so that a detached child
    // is not reparented before the final snapshot.
    let tracker_result = if root_exited {
        state.tracker.terminate(true, state.cleanup_timeout)
    } else if observation_failed || root_signal_failed {
        state.tracker.stop(state.cleanup_timeout)
    } else {
        state.tracker.supervise(state.cleanup_timeout)
    };
    cleanup_failed |= record_error(&mut error, tracker_result);

    if root_exited || observation_failed {
        cleanup_failed |= record_error(&mut error, stop_process_group(state.root));
    }
    if observation_failed || root_exited && error.is_some() {
        let root_signal = signal_process(state.root, libc::SIGKILL).map(|_| ());
        cleanup_failed |= record_error(&mut error, root_signal);
    }
    if root_exited && error.is_none() {
        record_error(&mut error, read_owner_control(&mut state.stream));
    }

    // A readiness error does not make otherwise successful cleanup unsafe.
    let remove_directory = if startup_failed {
        !cleanup_failed
    } else {
        error.is_none()
    };
    if remove_directory {
        state.temporary_directory.remove();
    } else {
        state.temporary_directory.preserve();
    }

    error.map_or(Ok(()), Err)
}

fn record_error(error: &mut Option<String>, result: Result<(), String>) -> bool {
    let Err(next_error) = result else {
        return false;
    };
    *error = Some(with_prior_error(error.take(), next_error));
    true
}
