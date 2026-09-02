use super::super::process::{ProcessIdentity, process_info, signal_process};
use super::super::process_tracker::DescendantTracker;
use super::{protocol, stop_process_group, with_prior_error};
use crate::sandbox::platform;
use std::io::{Read, Write};
use std::net::Shutdown;
use std::os::fd::FromRawFd;
use std::os::unix::net::UnixStream;
use std::sync::{Arc, Mutex};
use std::time::Duration;

type GroupBackstop = Mutex<bool>;

pub(super) fn run() -> Result<(), String> {
    // SAFETY: the owner transfers its private control socket as the manager's
    // standard input and retains no manager-side copy after spawning.
    let mut stream = unsafe { UnixStream::from_raw_fd(libc::STDIN_FILENO) };
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
    let group_backstop = Arc::new(GroupBackstop::new(false));
    let tracker_group_backstop = Arc::clone(&group_backstop);
    let tracker_thread = std::thread::spawn(move || {
        supervise_tracker(
            tracker,
            cleanup_timeout,
            root,
            tracker_control,
            tracker_group_backstop,
        )
    });
    if let Err(error) = stream.write_all(&[protocol::COMMITTED]) {
        return finish_committed_startup_failure(
            format!("failed to confirm sandbox manager ownership: {error}"),
            root,
            &stream,
            tracker_thread,
            &group_backstop,
            temporary_directory,
        );
    }
    let disposition = protocol::read_owner_disposition(&mut stream);
    if matches!(disposition, protocol::OwnerDisposition::RetirementStarted) {
        return finish_retirement(
            root,
            &mut stream,
            tracker_thread,
            &group_backstop,
            temporary_directory,
        );
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
    let root_exit_expected = if stop_root {
        request_root_stop(root, run_group_backstop(root, &group_backstop), &mut error)
            .root_exit_expected
    } else {
        true
    };
    if root_exit_expected {
        if let Err(tracker_error) = join_tracker(tracker_thread) {
            error = Some(with_prior_error(error, tracker_error));
        }
    } else {
        // The tracker cannot finish while an unkillable root remains live.
        // Returning ends this manager process and its detached tracker thread.
        drop(tracker_thread);
    }
    if let Err(group_error) = run_group_backstop(root, &group_backstop) {
        error = Some(with_prior_error(error, group_error));
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
    group_backstop: &GroupBackstop,
    temporary_directory: platform::TemporaryDirectory,
) -> Result<(), String> {
    let mut error = join_tracker(tracker_thread).err();
    if let Err(group_error) = run_group_backstop(root, group_backstop) {
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
    } else {
        drop(tracker);
    }
    if cleanup_failed {
        temporary_directory.preserve();
    }
    Err(error.expect("startup failure should retain its error"))
}

fn supervise_tracker(
    tracker: DescendantTracker,
    cleanup_timeout: Duration,
    root: ProcessIdentity,
    control: UnixStream,
    group_backstop: Arc<GroupBackstop>,
) -> Result<(), String> {
    let mut error = tracker.supervise(cleanup_timeout).err();
    if let Err(group_error) = run_group_backstop(root, &group_backstop) {
        error = Some(with_prior_error(error, group_error));
    }
    let Some(mut error) = error else {
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
    error: String,
    root: ProcessIdentity,
    control: &UnixStream,
    tracker_thread: std::thread::JoinHandle<Result<(), String>>,
    group_backstop: &GroupBackstop,
    temporary_directory: platform::TemporaryDirectory,
) -> Result<(), String> {
    let mut error = Some(error);
    let stop = request_root_stop(root, run_group_backstop(root, group_backstop), &mut error);
    let mut cleanup_failed = stop.group_cleanup_failed || !stop.root_exit_expected;
    if let Err(control_error) = control.shutdown(Shutdown::Both) {
        error = Some(with_prior_error(
            error,
            format!("failed to close sandbox manager control: {control_error}"),
        ));
    }
    if stop.root_exit_expected {
        if let Err(cleanup_error) = join_tracker(tracker_thread) {
            error = Some(with_prior_error(error, cleanup_error));
            cleanup_failed = true;
        }
    } else {
        drop(tracker_thread);
    }
    if cleanup_failed {
        temporary_directory.preserve();
    }
    Err(error.expect("committed startup failure should retain its error"))
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

fn run_group_backstop(root: ProcessIdentity, group_backstop: &GroupBackstop) -> Result<(), String> {
    // Tracker completion and owner loss can race. The first caller propagates
    // failure from its validated root; do not retry across that identity boundary.
    let mut started = group_backstop
        .lock()
        .map_err(|_| "sandbox manager group backstop state was poisoned".to_string())?;
    if *started {
        return Ok(());
    }
    *started = true;
    stop_process_group(root)
}

fn join_tracker(tracker_thread: std::thread::JoinHandle<Result<(), String>>) -> Result<(), String> {
    tracker_thread
        .join()
        .map_err(|_| "sandbox manager process tracker failed".to_string())
        .and_then(|result| result)
}
