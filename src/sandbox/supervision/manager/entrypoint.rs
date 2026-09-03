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
    let mut stream = unsafe { UnixStream::from_raw_fd(libc::STDIN_FILENO) };

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

    let mut tracker =
        DescendantTracker::start(root_pid).map_err(|failure| failure.retire(cleanup_timeout))?;
    let temporary_directory = platform::TemporaryDirectory::adopt(temporary_directory, owner_pid)?;
    let root = info.identity;

    if let Err(error) = tracker.watch_control(stream.as_raw_fd()) {
        return finish_startup_failure(error, root, tracker, temporary_directory, cleanup_timeout);
    }
    if let Err(error) = stream.write_all(&[READY]) {
        tracker.remove_control_watch();
        return finish_startup_failure(
            format!("failed to report sandbox manager readiness: {error}"),
            root,
            tracker,
            temporary_directory,
            cleanup_timeout,
        );
    }

    let observation = observe_lifetime(&mut stream, &mut tracker);
    tracker.remove_control_watch();
    let result = match observation {
        Ok(Retirement::RootExited(control_error)) => {
            finish_root_exit(root, tracker, cleanup_timeout, control_error)
                .and_then(|()| read_owner_control(&mut stream))
        }
        Ok(Retirement::OwnerLost(control_error)) => {
            finish_owner_loss(root, tracker, cleanup_timeout, control_error)
        }
        Err(error) => finish_observation_failure(error, root, tracker, cleanup_timeout),
    };
    if result.is_err() {
        temporary_directory.preserve();
    } else {
        temporary_directory.remove();
    }
    result
}

enum Retirement {
    OwnerLost(Option<String>),
    RootExited(Option<String>),
}

fn observe_lifetime(
    stream: &mut UnixStream,
    tracker: &mut DescendantTracker,
) -> Result<Retirement, String> {
    loop {
        if tracker.root_has_exited()? {
            return Ok(Retirement::RootExited(None));
        }
        match tracker.wait_for_events(None)? {
            EventWait::Events(events) => {
                let control_error = if events.control_readable {
                    read_owner_control(stream).err()
                } else {
                    None
                };
                if events.root_exited || events.control_readable && tracker.root_has_exited()? {
                    return Ok(Retirement::RootExited(control_error));
                }
                if events.control_readable {
                    return Ok(Retirement::OwnerLost(control_error));
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

fn finish_startup_failure(
    error: String,
    root: ProcessIdentity,
    tracker: DescendantTracker,
    temporary_directory: platform::TemporaryDirectory,
    cleanup_timeout: Duration,
) -> Result<(), String> {
    let mut error = Some(error);
    let stop = request_root_stop(root, stop_process_group(root), &mut error);
    let mut cleanup_failed = stop.group_cleanup_failed || !stop.root_exit_expected;
    if stop.root_exit_expected {
        if let Err(cleanup_error) = tracker.supervise(cleanup_timeout) {
            error = Some(with_prior_error(error, cleanup_error));
            cleanup_failed = true;
        }
    } else if let Err(cleanup_error) = tracker.stop(cleanup_timeout) {
        error = Some(with_prior_error(error, cleanup_error));
        cleanup_failed = true;
    }
    if cleanup_failed {
        temporary_directory.preserve();
    } else {
        temporary_directory.remove();
    }
    Err(error.expect("startup failure should retain its error"))
}

fn finish_root_exit(
    root: ProcessIdentity,
    tracker: DescendantTracker,
    cleanup_timeout: Duration,
    mut error: Option<String>,
) -> Result<(), String> {
    if let Err(cleanup_error) = tracker.terminate(true, cleanup_timeout) {
        error = Some(with_prior_error(error, cleanup_error));
    }
    if let Err(group_error) = stop_process_group(root) {
        error = Some(with_prior_error(error, group_error));
    }
    if error.is_some()
        && let Err(signal_error) = signal_process(root, libc::SIGKILL)
    {
        error = Some(with_prior_error(error, signal_error));
    }
    error.map_or(Ok(()), Err)
}

fn finish_owner_loss(
    root: ProcessIdentity,
    tracker: DescendantTracker,
    cleanup_timeout: Duration,
    mut error: Option<String>,
) -> Result<(), String> {
    let stop = request_root_stop(root, stop_process_group(root), &mut error);
    let tracker_result = if stop.root_exit_expected {
        tracker.supervise(cleanup_timeout)
    } else {
        tracker.stop(cleanup_timeout)
    };
    if let Err(cleanup_error) = tracker_result {
        error = Some(with_prior_error(error, cleanup_error));
    }
    error.map_or(Ok(()), Err)
}

fn finish_observation_failure(
    observation_error: String,
    root: ProcessIdentity,
    tracker: DescendantTracker,
    cleanup_timeout: Duration,
) -> Result<(), String> {
    let mut error = Some(observation_error);
    if let Err(cleanup_error) = tracker.terminate(false, cleanup_timeout) {
        error = Some(with_prior_error(error, cleanup_error));
    }
    if let Err(group_error) = stop_process_group(root) {
        error = Some(with_prior_error(error, group_error));
    }
    if let Err(signal_error) = signal_process(root, libc::SIGKILL) {
        error = Some(with_prior_error(error, signal_error));
    }
    Err(error.expect("observation failure should retain its error"))
}

struct RootStop {
    root_exit_expected: bool,
    group_cleanup_failed: bool,
}

fn request_root_stop(
    root: ProcessIdentity,
    group_result: Result<(), String>,
    error: &mut Option<String>,
) -> RootStop {
    let group_cleanup_failed = match group_result {
        Ok(()) => false,
        Err(group_error) => {
            *error = Some(with_prior_error(error.take(), group_error));
            true
        }
    };
    let root_signal_failed = match signal_process(root, libc::SIGKILL) {
        Ok(_) => false,
        Err(signal_error) => {
            *error = Some(with_prior_error(error.take(), signal_error));
            true
        }
    };
    RootStop {
        root_exit_expected: !root_signal_failed,
        group_cleanup_failed,
    }
}
