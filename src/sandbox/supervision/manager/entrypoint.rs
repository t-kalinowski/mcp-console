use super::super::process::{ProcessIdentity, process_info};
use super::super::process_tracker::{DescendantTracker, EventWait};
use super::super::process_tree::PROCESS_REAP_EVENT;
use super::protocol;
use std::fs;
use std::io::Write;
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::time::Duration;

const READY: u8 = 1;

pub(super) fn run() -> Result<(), String> {
    let mut stream = inherited_control();
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
    let owner = process_info(owner_pid)?
        .filter(|info| !info.is_zombie)
        .ok_or_else(|| format!("sandbox manager owner {owner_pid} exited before startup"))?
        .identity;
    let root_info = process_info(root_pid)?
        .ok_or_else(|| format!("sandbox root {root_pid} exited before manager startup"))?;
    if root_info.parent_pid != owner_pid {
        return Err(format!(
            "sandbox root {root_pid} is not a child of manager owner {owner_pid}"
        ));
    }
    let tracker =
        DescendantTracker::start(root_pid).map_err(|failure| failure.retire(cleanup_timeout))?;
    let temporary_directory = match AdoptedTemporaryDirectory::adopt(temporary_directory, owner_pid)
    {
        Ok(directory) => directory,
        Err(error) => {
            return with_cleanup(error, tracker, false, cleanup_timeout);
        }
    };
    if let Err(error) = register_owner_exit(&tracker, owner) {
        return finish_startup_failure(error, tracker, temporary_directory, cleanup_timeout);
    }
    if let Err(error) = stream.write_all(&[READY]) {
        return finish_startup_failure(
            format!("failed to report sandbox manager readiness: {error}"),
            tracker,
            temporary_directory,
            cleanup_timeout,
        );
    }
    match supervise_owner(tracker, owner, &mut stream, cleanup_timeout) {
        Ok(TemporaryDirectoryDisposition::Remove) => Ok(()),
        Ok(TemporaryDirectoryDisposition::Preserve) => {
            temporary_directory.preserve();
            Ok(())
        }
        Err(error) => {
            temporary_directory.preserve();
            Err(error)
        }
    }
}

enum TemporaryDirectoryDisposition {
    Remove,
    Preserve,
}

fn supervise_owner(
    mut tracker: DescendantTracker,
    owner: ProcessIdentity,
    stream: &mut UnixStream,
    cleanup_timeout: Duration,
) -> Result<TemporaryDirectoryDisposition, String> {
    loop {
        match identity_is_live(owner) {
            Ok(false) => {
                finish_tracker(tracker, false, cleanup_timeout)?;
                return await_temporary_directory_disposition(stream);
            }
            Ok(true) => {}
            Err(error) => {
                return with_cleanup(error, tracker, false, cleanup_timeout);
            }
        }
        match tracker.root_has_exited() {
            Ok(true) => {
                finish_tracker(tracker, true, cleanup_timeout)?;
                return await_temporary_directory_disposition(stream);
            }
            Ok(false) => {}
            Err(error) => {
                return with_cleanup(error, tracker, false, cleanup_timeout);
            }
        }

        match tracker.wait_for_events(None) {
            Ok(EventWait::Events | EventWait::RootExited) => {}
            Ok(EventWait::TimedOut) => {
                return with_cleanup(
                    "sandbox manager process wait unexpectedly timed out".to_string(),
                    tracker,
                    false,
                    cleanup_timeout,
                );
            }
            Err(error) => {
                return with_cleanup(error, tracker, false, cleanup_timeout);
            }
        }
    }
}

fn await_temporary_directory_disposition(
    stream: &mut UnixStream,
) -> Result<TemporaryDirectoryDisposition, String> {
    let mut retirement_started = false;
    loop {
        match protocol::read_retirement_command(stream)? {
            Some(protocol::RetirementCommand::Started) if !retirement_started => {
                retirement_started = true;
                if protocol::write_cleanup_complete(stream).is_err() {
                    return Ok(TemporaryDirectoryDisposition::Preserve);
                }
            }
            Some(protocol::RetirementCommand::Started) => {
                return Err("sandbox manager received duplicate retirement start".to_string());
            }
            Some(protocol::RetirementCommand::RemoveTemporaryDirectory) if retirement_started => {
                return Ok(TemporaryDirectoryDisposition::Remove);
            }
            Some(protocol::RetirementCommand::PreserveTemporaryDirectory) if retirement_started => {
                return Ok(TemporaryDirectoryDisposition::Preserve);
            }
            Some(
                protocol::RetirementCommand::RemoveTemporaryDirectory
                | protocol::RetirementCommand::PreserveTemporaryDirectory,
            ) => {
                return Err(
                    "sandbox manager received a disposition before retirement started".to_string(),
                );
            }
            None if retirement_started => {
                return Ok(TemporaryDirectoryDisposition::Preserve);
            }
            None => return Ok(TemporaryDirectoryDisposition::Remove),
        }
    }
}

fn register_owner_exit(tracker: &DescendantTracker, owner: ProcessIdentity) -> Result<(), String> {
    let event = libc::kevent {
        ident: owner.pid as libc::uintptr_t,
        filter: libc::EVFILT_PROC,
        flags: libc::EV_ADD | libc::EV_CLEAR,
        fflags: libc::NOTE_EXIT | PROCESS_REAP_EVENT,
        data: 0,
        udata: std::ptr::null_mut(),
    };
    loop {
        // SAFETY: the kqueue descriptor is live, `event` is initialized, and
        // this submission supplies no output buffer.
        let result = unsafe {
            libc::kevent(
                tracker.kqueue.as_raw_fd(),
                &event,
                1,
                std::ptr::null_mut(),
                0,
                std::ptr::null(),
            )
        };
        if result >= 0 {
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(format!("failed to observe sandbox manager owner: {error}"));
        }
    }
}

fn identity_is_live(identity: ProcessIdentity) -> Result<bool, String> {
    Ok(
        process_info(identity.pid)?
            .is_some_and(|info| info.identity == identity && !info.is_zombie),
    )
}

fn with_cleanup<T>(
    error: String,
    tracker: DescendantTracker,
    root_exited: bool,
    cleanup_timeout: Duration,
) -> Result<T, String> {
    match finish_tracker(tracker, root_exited, cleanup_timeout) {
        Ok(()) => Err(error),
        Err(cleanup_error) => Err(format!("{error}; additionally, {cleanup_error}")),
    }
}

fn finish_tracker(
    tracker: DescendantTracker,
    root_exited: bool,
    cleanup_timeout: Duration,
) -> Result<(), String> {
    tracker.terminate(root_exited, cleanup_timeout)
}

fn finish_startup_failure(
    error: String,
    tracker: DescendantTracker,
    temporary_directory: AdoptedTemporaryDirectory,
    cleanup_timeout: Duration,
) -> Result<(), String> {
    let result = with_cleanup(error, tracker, false, cleanup_timeout);
    if result.is_err() {
        temporary_directory.preserve();
    }
    result
}

fn inherited_control() -> UnixStream {
    // SAFETY: the hidden manager entry point is launched with its owned control
    // stream on fd 0 and does not otherwise use standard input.
    unsafe { UnixStream::from_raw_fd(libc::STDIN_FILENO) }
}

struct AdoptedTemporaryDirectory(PathBuf);

impl AdoptedTemporaryDirectory {
    fn adopt(path: PathBuf, owner_pid: libc::pid_t) -> Result<Self, String> {
        let path = path.canonicalize().map_err(|error| {
            format!(
                "failed to resolve sandbox temporary directory {}: {error}",
                path.display()
            )
        })?;
        let expected_prefix = format!("mcp-console-tmp-{owner_pid}-");
        let valid_name = path
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.starts_with(&expected_prefix));
        let expected_parent = std::env::temp_dir().canonicalize().map_err(|error| {
            format!("failed to resolve the system temporary directory: {error}")
        })?;
        if !valid_name || path.parent() != Some(expected_parent.as_path()) || !path.is_dir() {
            return Err("sandbox temporary directory has invalid ownership".to_string());
        }
        Ok(Self(path))
    }

    fn preserve(self) {
        std::mem::forget(self);
    }
}

impl Drop for AdoptedTemporaryDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
