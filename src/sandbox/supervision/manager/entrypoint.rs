use super::super::process::{ProcessIdentity, process_info, signal_process};
use super::super::process_tracker::DescendantTracker;
use super::{CONTROL_DESCRIPTOR_ENV, protocol, stop_process_group, with_prior_error};
use crate::sandbox::platform;
use std::io::{Read, Write};
use std::net::Shutdown;
use std::os::fd::FromRawFd;
use std::os::unix::net::UnixStream;
use std::time::Duration;

pub(super) fn run() -> Result<(), String> {
    let mut stream = inherited_control()?;
    let protocol::Initialization {
        owner_pid,
        root_pid,
        cleanup_timeout,
        temporary_directory,
    } = protocol::read(&mut stream)?;

    // SAFETY: getppid(2) has no pointer or lifetime preconditions.
    let parent_pid = unsafe { libc::getppid() };
    if parent_pid != owner_pid {
        return Err(format!(
            "sandbox manager owner changed before commitment: expected {owner_pid}, found {parent_pid}"
        ));
    }
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
    let root = info.identity;

    if let Err(error) = stream.write_all(&[protocol::READY]) {
        return finish_startup_failure(
            format!("failed to report sandbox manager readiness: {error}"),
            root,
            tracker,
            temporary_directory,
            cleanup_timeout,
        );
    }

    let mut commit = [0];
    if let Err(error) = stream.read_exact(&mut commit) {
        return finish_startup_failure(
            format!("sandbox manager ownership was not committed: {error}"),
            root,
            tracker,
            temporary_directory,
            cleanup_timeout,
        );
    }
    if commit != [protocol::COMMIT] {
        return finish_startup_failure(
            "sandbox manager ownership commit is invalid".to_string(),
            root,
            tracker,
            temporary_directory,
            cleanup_timeout,
        );
    }

    let tracker_control = match stream.try_clone() {
        Ok(control) => control,
        Err(error) => {
            return finish_startup_failure(
                format!("failed to monitor sandbox manager control: {error}"),
                root,
                tracker,
                temporary_directory,
                cleanup_timeout,
            );
        }
    };
    let tracker_thread = std::thread::spawn(move || {
        supervise_tracker(tracker, cleanup_timeout, root, tracker_control)
    });
    if let Err(error) = stream.write_all(&[protocol::COMMITTED]) {
        return finish_committed_startup_failure(
            format!("failed to confirm sandbox manager ownership: {error}"),
            root,
            &stream,
            tracker_thread,
            temporary_directory,
        );
    }
    let disposition = protocol::read_owner_disposition(&mut stream);
    if matches!(disposition, protocol::OwnerDisposition::RetirementStarted) {
        return finish_retirement(root, &mut stream, tracker_thread, temporary_directory);
    }
    let stop_root = !matches!(&disposition, protocol::OwnerDisposition::Finish);
    let mut error = match disposition {
        protocol::OwnerDisposition::Finish
        | protocol::OwnerDisposition::Stop
        | protocol::OwnerDisposition::Closed => None,
        protocol::OwnerDisposition::RemoveTemporaryDirectory
        | protocol::OwnerDisposition::PreserveTemporaryDirectory => {
            Some("sandbox manager received a disposition before retirement started".to_string())
        }
        protocol::OwnerDisposition::RetirementStarted => unreachable!(),
        protocol::OwnerDisposition::Failed(error) => Some(error),
    };
    if stop_root {
        if let Err(group_error) = stop_process_group(root) {
            error = Some(with_prior_error(error, group_error));
        }
        if let Err(signal_error) = signal_process(root, libc::SIGKILL) {
            error = Some(with_prior_error(error, signal_error));
        }
        if let Err(tracker_error) = join_tracker(tracker_thread) {
            error = Some(with_prior_error(error, tracker_error));
        }
    } else {
        if let Err(tracker_error) = join_tracker(tracker_thread) {
            error = Some(with_prior_error(error, tracker_error));
        }
        if let Err(group_error) = stop_process_group(root) {
            error = Some(with_prior_error(error, group_error));
        }
    }
    if error.is_some() {
        temporary_directory.preserve();
    }
    error.map_or(Ok(()), Err)
}

fn finish_retirement(
    root: ProcessIdentity,
    stream: &mut UnixStream,
    tracker_thread: std::thread::JoinHandle<Result<(), String>>,
    temporary_directory: platform::TemporaryDirectory,
) -> Result<(), String> {
    let mut error = join_tracker(tracker_thread).err();
    if let Err(group_error) = stop_process_group(root) {
        error = Some(with_prior_error(error, group_error));
    }
    if let Some(error) = error {
        temporary_directory.preserve();
        return Err(error);
    }

    if protocol::write_cleanup_complete(stream).is_err() {
        temporary_directory.preserve();
        return Ok(());
    }
    match protocol::read_owner_disposition(stream) {
        protocol::OwnerDisposition::RemoveTemporaryDirectory => Ok(()),
        protocol::OwnerDisposition::PreserveTemporaryDirectory
        | protocol::OwnerDisposition::Closed => {
            temporary_directory.preserve();
            Ok(())
        }
        protocol::OwnerDisposition::Failed(error) => {
            temporary_directory.preserve();
            Err(error)
        }
        protocol::OwnerDisposition::Finish
        | protocol::OwnerDisposition::Stop
        | protocol::OwnerDisposition::RetirementStarted => {
            temporary_directory.preserve();
            Err("sandbox manager received an invalid retirement disposition".to_string())
        }
    }
}

fn finish_startup_failure(
    mut error: String,
    root: ProcessIdentity,
    tracker: DescendantTracker,
    temporary_directory: platform::TemporaryDirectory,
    cleanup_timeout: Duration,
) -> Result<(), String> {
    let mut cleanup_failed = false;
    if let Err(group_error) = stop_process_group(root) {
        error.push_str(&format!("; additionally, {group_error}"));
        cleanup_failed = true;
    }
    if let Err(signal_error) = signal_process(root, libc::SIGKILL) {
        error.push_str(&format!("; additionally, {signal_error}"));
    }
    if let Err(cleanup_error) = tracker.supervise(cleanup_timeout) {
        error.push_str(&format!("; additionally, {cleanup_error}"));
        cleanup_failed = true;
    }
    if cleanup_failed {
        temporary_directory.preserve();
    }
    Err(error)
}

fn supervise_tracker(
    tracker: DescendantTracker,
    cleanup_timeout: Duration,
    root: ProcessIdentity,
    control: UnixStream,
) -> Result<(), String> {
    let Err(mut error) = tracker.supervise(cleanup_timeout) else {
        return Ok(());
    };
    if let Err(signal_error) = signal_process(root, libc::SIGKILL) {
        error = with_prior_error(Some(error), signal_error);
    }
    if let Err(control_error) = control.shutdown(Shutdown::Both) {
        error = with_prior_error(
            Some(error),
            format!("failed to close sandbox manager control: {control_error}"),
        );
    }
    Err(error)
}

fn finish_committed_startup_failure(
    mut error: String,
    root: ProcessIdentity,
    control: &UnixStream,
    tracker_thread: std::thread::JoinHandle<Result<(), String>>,
    temporary_directory: platform::TemporaryDirectory,
) -> Result<(), String> {
    let mut cleanup_failed = false;
    if let Err(group_error) = stop_process_group(root) {
        error = with_prior_error(Some(error), group_error);
        cleanup_failed = true;
    }
    if let Err(signal_error) = signal_process(root, libc::SIGKILL) {
        error = with_prior_error(Some(error), signal_error);
    }
    if let Err(control_error) = control.shutdown(Shutdown::Both) {
        error = with_prior_error(
            Some(error),
            format!("failed to close sandbox manager control: {control_error}"),
        );
    }
    if let Err(cleanup_error) = join_tracker(tracker_thread) {
        error = with_prior_error(Some(error), cleanup_error);
        cleanup_failed = true;
    }
    if cleanup_failed {
        temporary_directory.preserve();
    }
    Err(error)
}

fn join_tracker(tracker_thread: std::thread::JoinHandle<Result<(), String>>) -> Result<(), String> {
    tracker_thread
        .join()
        .map_err(|_| "sandbox manager process tracker failed".to_string())
        .and_then(|result| result)
}

fn inherited_control() -> Result<UnixStream, String> {
    let descriptor = std::env::var(CONTROL_DESCRIPTOR_ENV)
        .map_err(|_| "sandbox manager control descriptor is missing".to_string())?
        .parse::<libc::c_int>()
        .ok()
        .filter(|descriptor| *descriptor > libc::STDERR_FILENO)
        .ok_or_else(|| "sandbox manager control descriptor is invalid".to_string())?;
    // SAFETY: the descriptor is inherited exclusively by this manager process.
    Ok(unsafe { UnixStream::from_raw_fd(descriptor) })
}
