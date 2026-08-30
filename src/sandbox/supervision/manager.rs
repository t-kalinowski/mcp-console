use super::process::{ProcessIdentity, process_info, signal_process};
use super::process_tracker::DescendantTracker;
use crate::sandbox::file_descriptors::configure as configure_file_descriptors;
use crate::sandbox::platform;
use std::ffi::OsString;
use std::io::{ErrorKind, Read, Write};
use std::net::Shutdown;
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt as _;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread::JoinHandle;
use std::time::Duration;

const CONTROL_DESCRIPTOR_ENV: &str = "MCP_CONSOLE_SANDBOX_MANAGER_FD";
const INITIALIZATION_MAGIC: &[u8; 4] = b"MCG2";
const READY: u8 = 1;
const COMMIT: u8 = 2;
const FINISH: u8 = 3;
const STOP: u8 = 5;
const COMMITTED: u8 = 7;
const MAXIMUM_PATH_BYTES: usize = 16 * 1024;
const STARTUP_TIMEOUT: Duration = Duration::from_secs(5);
const FINISH_ALLOWANCE: Duration = Duration::from_secs(1);

pub(crate) struct SandboxManager {
    child: Option<Child>,
    child_identity: Option<ProcessIdentity>,
    monitor: Option<ManagerMonitor>,
    stream: Option<UnixStream>,
    cleanup_timeout: Duration,
}

struct ManagerMonitor {
    identity: ProcessIdentity,
    result: Receiver<Result<ManagerExit, String>>,
    thread: Option<JoinHandle<()>>,
}

enum ManagerExit {
    Normal,
    Recovered,
}

impl SandboxManager {
    pub(crate) fn spawn(cleanup_timeout: Duration) -> Result<Self, String> {
        let _ = cleanup_timeout_millis(cleanup_timeout)?;
        let executable = std::env::current_exe()
            .map_err(|error| format!("failed to locate the sandbox manager: {error}"))?;
        let (stream, inherited_stream) = UnixStream::pair()
            .map_err(|error| format!("failed to create sandbox manager control: {error}"))?;
        let inherited_descriptor = inherited_stream.as_raw_fd();

        let mut command = Command::new(executable);
        command
            .arg("sandbox-manager")
            .env(CONTROL_DESCRIPTOR_ENV, inherited_descriptor.to_string())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0);
        configure_file_descriptors(&mut command, vec![inherited_descriptor])?;

        let mut child = command
            .spawn()
            .map_err(|error| format!("failed to launch the sandbox manager: {error}"))?;
        let child_pid = child.id() as libc::pid_t;
        let child_identity = match process_info(child_pid) {
            Ok(Some(info)) => info.identity,
            Ok(None) => {
                return Err(stop_and_reap(
                    &mut child,
                    "sandbox manager exited before it could be monitored".to_string(),
                ));
            }
            Err(error) => {
                return Err(stop_and_reap(
                    &mut child,
                    format!("failed to inspect sandbox manager: {error}"),
                ));
            }
        };
        drop(inherited_stream);
        Ok(Self {
            child: Some(child),
            child_identity: Some(child_identity),
            monitor: None,
            stream: Some(stream),
            cleanup_timeout,
        })
    }

    pub(crate) fn observe(
        &mut self,
        root_pid: u32,
        temporary_directory: &Path,
    ) -> Result<(), String> {
        let root_pid = libc::pid_t::try_from(root_pid)
            .ok()
            .filter(|pid| *pid > 0)
            .ok_or_else(|| "sandbox manager received an invalid root PID".to_string())?;
        let path = temporary_directory.as_os_str().as_bytes();
        let path_length = u32::try_from(path.len())
            .ok()
            .filter(|length| *length as usize <= MAXIMUM_PATH_BYTES)
            .ok_or_else(|| "sandbox manager temporary path is too long".to_string())?;
        let cleanup_timeout = cleanup_timeout_millis(self.cleanup_timeout)?;
        let stream = self
            .stream
            .as_mut()
            .expect("sandbox manager control should be available");
        stream
            .set_read_timeout(Some(STARTUP_TIMEOUT))
            .map_err(|error| format!("failed to configure sandbox manager control: {error}"))?;
        stream
            .set_write_timeout(Some(STARTUP_TIMEOUT))
            .map_err(|error| format!("failed to configure sandbox manager control: {error}"))?;

        stream
            .write_all(INITIALIZATION_MAGIC)
            .and_then(|()| stream.write_all(&root_pid.to_be_bytes()))
            .and_then(|()| stream.write_all(&cleanup_timeout.to_be_bytes()))
            .and_then(|()| stream.write_all(&path_length.to_be_bytes()))
            .and_then(|()| stream.write_all(path))
            .map_err(|error| format!("failed to initialize sandbox manager: {error}"))?;

        let mut ready = [0];
        stream
            .read_exact(&mut ready)
            .map_err(|error| format!("sandbox manager did not become ready: {error}"))?;
        if ready != [READY] {
            return Err("sandbox manager sent an invalid readiness response".to_string());
        }
        Ok(())
    }

    /// Watches a committed manager and takes over cleanup only if it exits
    /// unsuccessfully while the sandbox root remains live. The direct child
    /// remains waitable in its owner, so its PID cannot be reused while this
    /// monitor reconstructs the root's current process tree.
    pub(crate) fn monitor(
        &mut self,
        root_pid: u32,
        temporary_directory: platform::TemporaryDirectory,
    ) {
        assert!(
            self.monitor.is_none(),
            "sandbox manager can be monitored only once"
        );
        let child = self
            .child
            .take()
            .expect("committed sandbox manager should remain waitable");
        let child_identity = self
            .child_identity
            .take()
            .expect("committed sandbox manager identity should remain pinned");
        let root_pid = root_pid as libc::pid_t;
        assert!(root_pid > 0, "sandbox root PID should be valid");
        self.monitor = Some(ManagerMonitor::start(
            child,
            child_identity,
            root_pid,
            temporary_directory,
            self.cleanup_timeout,
        ));
    }

    pub(crate) fn commit(&mut self) -> Result<(), String> {
        let stream = self
            .stream
            .as_mut()
            .expect("sandbox manager control should be available");
        stream
            .write_all(&[COMMIT])
            .map_err(|error| format!("failed to commit sandbox manager ownership: {error}"))?;
        let mut committed = [0];
        stream
            .read_exact(&mut committed)
            .map_err(|error| format!("sandbox manager did not confirm ownership: {error}"))?;
        if committed != [COMMITTED] {
            return Err("sandbox manager sent an invalid ownership confirmation".to_string());
        }
        Ok(())
    }

    /// Completes ownership after the sandbox root has already exited.
    pub(crate) fn finish(mut self) -> Result<(), String> {
        self.finish_inner(Some(FINISH), true)
    }

    /// Stops the recorded sandbox root before completing ownership.
    pub(crate) fn stop(mut self) -> Result<(), String> {
        self.finish_inner(Some(STOP), true)
    }

    fn finish_inner(
        &mut self,
        disposition: Option<u8>,
        inspect_status: bool,
    ) -> Result<(), String> {
        let mut error = None;
        if let Some(mut stream) = self.stream.take()
            && let Some(disposition) = disposition
            && let Err(write_error) = stream.write_all(&[disposition])
        {
            error = Some(format!(
                "failed to finish sandbox manager ownership: {write_error}"
            ));
        }
        let finish_timeout = self.cleanup_timeout.saturating_add(FINISH_ALLOWANCE);
        if let Some(monitor) = self.monitor.take() {
            match monitor.finish(finish_timeout) {
                Ok(ManagerExit::Normal) => {}
                Ok(ManagerExit::Recovered) => error = None,
                Err(monitor_error) => {
                    error = Some(with_prior_error(error, monitor_error));
                }
            }
            return error.map_or(Ok(()), Err);
        }
        let Some(mut child) = self.child.take() else {
            return error.map_or(Ok(()), Err);
        };
        let exited =
            match platform::wait_for_process_exit_without_reaping(child.id(), finish_timeout) {
                Ok(exited) => exited,
                Err(wait_error) => {
                    let wait_error = stop_and_reap(
                        &mut child,
                        format!("failed to wait for sandbox manager: {wait_error}"),
                    );
                    return Err(with_prior_error(error, wait_error));
                }
            };
        if !exited {
            let timeout = stop_and_reap(
                &mut child,
                "timed out waiting for sandbox manager cleanup".to_string(),
            );
            return Err(with_prior_error(error, timeout));
        }
        let status = match child.wait() {
            Ok(status) => status,
            Err(wait_error) => {
                let wait_error = format!("failed to reap sandbox manager: {wait_error}");
                return Err(with_prior_error(error, wait_error));
            }
        };
        if inspect_status && !status.success() {
            let status_error = format!("sandbox manager exited with status {status}");
            return Err(with_prior_error(error, status_error));
        }
        error.map_or(Ok(()), Err)
    }
}

impl ManagerMonitor {
    fn start(
        child: Child,
        identity: ProcessIdentity,
        root_pid: libc::pid_t,
        temporary_directory: platform::TemporaryDirectory,
        cleanup_timeout: Duration,
    ) -> Self {
        let (result_sender, result) = mpsc::channel();
        let thread = std::thread::spawn(move || {
            let result = monitor_manager(child, root_pid, temporary_directory, cleanup_timeout);
            let _ = result_sender.send(result);
        });
        Self {
            identity,
            result,
            thread: Some(thread),
        }
    }

    fn finish(mut self, timeout: Duration) -> Result<ManagerExit, String> {
        let mut error = None;
        let result = match self.result.recv_timeout(timeout) {
            Ok(result) => result,
            Err(RecvTimeoutError::Timeout) => {
                error = Some("timed out waiting for sandbox manager cleanup".to_string());
                if let Err(signal_error) = signal_process(self.identity, libc::SIGKILL) {
                    error = Some(with_prior_error(
                        error,
                        format!("failed to stop sandbox manager: {signal_error}"),
                    ));
                }
                // Do not release the caller's pinned sandbox root while this
                // thread still owns the exact, unreaped manager child. SIGKILL
                // makes the blocking waitid complete; any recovery tracker then
                // has its own cleanup deadline before it reports a result.
                self.result.recv().unwrap_or_else(|_| {
                    Err("sandbox manager monitor ended without a result".to_string())
                })
            }
            Err(RecvTimeoutError::Disconnected) => {
                Err("sandbox manager monitor ended without a result".to_string())
            }
        };
        let exit = match result {
            Ok(exit) => Some(exit),
            Err(result_error) => {
                error = Some(with_prior_error(error, result_error));
                None
            }
        };
        if self
            .thread
            .take()
            .expect("sandbox manager monitor thread should be joinable")
            .join()
            .is_err()
        {
            error = Some(with_prior_error(
                error,
                "sandbox manager monitor failed".to_string(),
            ));
        }
        match error {
            Some(error) => Err(error),
            None => Ok(exit.expect("successful manager monitor should report its exit")),
        }
    }
}

fn monitor_manager(
    mut child: Child,
    root_pid: libc::pid_t,
    temporary_directory: platform::TemporaryDirectory,
    cleanup_timeout: Duration,
) -> Result<ManagerExit, String> {
    let completion = match platform::wait_for_process_exit_without_reaping_blocking(child.id()) {
        Ok(()) => child
            .wait()
            .map_err(|wait_error| format!("failed to reap sandbox manager: {wait_error}")),
        Err(wait_error) => {
            let mut error = format!("failed to monitor sandbox manager: {wait_error}");
            if let Err(stop_error) = stop_manager_child(&mut child) {
                error = with_prior_error(Some(error), stop_error);
            }
            Err(error)
        }
    };

    match completion {
        Ok(status) if status.success() => Ok(ManagerExit::Normal),
        Ok(status) => finish_manager_failure(
            format!("sandbox manager exited with status {status}"),
            root_pid,
            temporary_directory,
            cleanup_timeout,
        ),
        Err(error) => finish_manager_failure(error, root_pid, temporary_directory, cleanup_timeout),
    }
}

fn stop_manager_child(child: &mut Child) -> Result<ExitStatus, String> {
    let mut error = None;
    if let Err(kill_error) = child.kill()
        && kill_error.raw_os_error() != Some(libc::ESRCH)
    {
        error = Some(format!("failed to stop sandbox manager: {kill_error}"));
    }
    let status = child
        .wait()
        .map_err(|wait_error| format!("failed to reap sandbox manager: {wait_error}"));
    match (error, status) {
        (None, Ok(status)) => Ok(status),
        (Some(error), Ok(_)) | (None, Err(error)) => Err(error),
        (Some(error), Err(wait_error)) => Err(with_prior_error(Some(error), wait_error)),
    }
}

fn finish_manager_failure(
    mut error: String,
    root_pid: libc::pid_t,
    temporary_directory: platform::TemporaryDirectory,
    cleanup_timeout: Duration,
) -> Result<ManagerExit, String> {
    let root = match process_info(root_pid) {
        Ok(Some(info)) if !info.is_zombie => info.identity,
        Ok(_) => {
            error = with_prior_error(
                Some(error),
                format!("sandbox root {root_pid} exited before fallback supervision"),
            );
            temporary_directory.preserve();
            return Err(error);
        }
        Err(inspect_error) => {
            error = with_prior_error(Some(error), inspect_error);
            temporary_directory.preserve();
            return Err(error);
        }
    };
    let cleanup =
        DescendantTracker::start(root_pid).and_then(|tracker| tracker.stop(cleanup_timeout));
    if let Err(cleanup_error) = cleanup {
        error = with_prior_error(Some(error), cleanup_error);
        if let Err(signal_error) = signal_process(root, libc::SIGKILL) {
            error = with_prior_error(Some(error), signal_error);
        }
        if let Err(group_error) = platform::kill_process_group(root_pid as u32) {
            error = with_prior_error(
                Some(error),
                format!("failed to stop sandbox process group: {group_error}"),
            );
        }
        temporary_directory.preserve();
        return Err(error);
    }
    Ok(ManagerExit::Recovered)
}

fn with_prior_error(prior: Option<String>, error: String) -> String {
    prior.map_or(error.clone(), |prior| {
        format!("{prior}; additionally, {error}")
    })
}

fn stop_and_reap(child: &mut Child, mut error: String) -> String {
    if let Err(kill_error) = child.kill()
        && kill_error.raw_os_error() != Some(libc::ESRCH)
    {
        error.push_str(&format!(
            "; additionally, failed to stop manager: {kill_error}"
        ));
    }
    if let Err(wait_error) = child.wait() {
        error.push_str(&format!(
            "; additionally, failed to reap manager: {wait_error}"
        ));
    }
    error
}

impl Drop for SandboxManager {
    fn drop(&mut self) {
        let _ = self.finish_inner(None, false);
    }
}

pub(super) fn run() -> Result<(), String> {
    // SAFETY: getppid(2) has no pointer or lifetime preconditions.
    let owner_pid = unsafe { libc::getppid() };
    if owner_pid <= 0 {
        return Err("sandbox manager has no valid owner".to_string());
    }

    let mut stream = inherited_control()?;
    let (root_pid, cleanup_timeout, temporary_directory) = read_initialization(&mut stream)?;
    let info = process_info(root_pid)?
        .ok_or_else(|| format!("sandbox root {root_pid} exited before manager startup"))?;
    if info.parent_pid != owner_pid {
        return Err(format!(
            "sandbox root {root_pid} is not a child of manager owner {owner_pid}"
        ));
    }

    let tracker = DescendantTracker::start(root_pid)?;
    let temporary_directory = platform::TemporaryDirectory::adopt(temporary_directory, owner_pid)?;
    let root = info.identity;

    if let Err(error) = stream.write_all(&[READY]) {
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
    if commit != [COMMIT] {
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
    if let Err(error) = stream.write_all(&[COMMITTED]) {
        return finish_committed_startup_failure(
            format!("failed to confirm sandbox manager ownership: {error}"),
            root,
            &stream,
            tracker_thread,
            temporary_directory,
        );
    }
    let disposition_error = read_owner_disposition(&mut stream, root);
    let tracker_result = join_tracker(tracker_thread);

    let mut error = disposition_error;
    if let Err(tracker_error) = tracker_result {
        error = Some(with_prior_error(error, tracker_error));
    }
    if error.is_some() {
        temporary_directory.preserve();
    }
    error.map_or(Ok(()), Err)
}

fn finish_startup_failure(
    mut error: String,
    root: super::process::ProcessIdentity,
    tracker: DescendantTracker,
    temporary_directory: platform::TemporaryDirectory,
    cleanup_timeout: Duration,
) -> Result<(), String> {
    if let Err(signal_error) = signal_process(root, libc::SIGKILL) {
        error.push_str(&format!("; additionally, {signal_error}"));
    }
    if let Err(cleanup_error) = tracker.supervise(cleanup_timeout) {
        error.push_str(&format!("; additionally, {cleanup_error}"));
        temporary_directory.preserve();
    }
    Err(error)
}

fn supervise_tracker(
    tracker: DescendantTracker,
    cleanup_timeout: Duration,
    root: super::process::ProcessIdentity,
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
    root: super::process::ProcessIdentity,
    control: &UnixStream,
    tracker_thread: std::thread::JoinHandle<Result<(), String>>,
    temporary_directory: platform::TemporaryDirectory,
) -> Result<(), String> {
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

fn read_owner_disposition(
    stream: &mut UnixStream,
    root: super::process::ProcessIdentity,
) -> Option<String> {
    let mut disposition = [0];
    loop {
        match stream.read(&mut disposition) {
            Ok(0) => {
                let error = signal_process(root, libc::SIGKILL).err();
                return error;
            }
            Ok(_) if disposition == [FINISH] => {
                return None;
            }
            Ok(_) if disposition == [STOP] => {
                return signal_process(root, libc::SIGKILL).err();
            }
            Ok(_) => {
                let mut error = "sandbox manager received an invalid finish request".to_string();
                if let Err(signal_error) = signal_process(root, libc::SIGKILL) {
                    error.push_str(&format!("; additionally, {signal_error}"));
                }
                return Some(error);
            }
            Err(error) if error.kind() == ErrorKind::Interrupted => {}
            Err(read_error) => {
                let mut error = format!("sandbox manager control failed: {read_error}");
                if let Err(signal_error) = signal_process(root, libc::SIGKILL) {
                    error.push_str(&format!("; additionally, {signal_error}"));
                }
                return Some(error);
            }
        }
    }
}

fn inherited_control() -> Result<UnixStream, String> {
    let descriptor = std::env::var(CONTROL_DESCRIPTOR_ENV)
        .map_err(|_| "sandbox manager control descriptor is missing".to_string())?
        .parse::<libc::c_int>()
        .ok()
        .filter(|descriptor| *descriptor > libc::STDERR_FILENO)
        .ok_or_else(|| "sandbox manager control descriptor is invalid".to_string())?;
    Ok(unsafe { UnixStream::from_raw_fd(descriptor) })
}

fn read_initialization(
    stream: &mut UnixStream,
) -> Result<(libc::pid_t, Duration, PathBuf), String> {
    let mut magic = [0; INITIALIZATION_MAGIC.len()];
    stream
        .read_exact(&mut magic)
        .map_err(|error| format!("failed to read sandbox manager initialization: {error}"))?;
    if &magic != INITIALIZATION_MAGIC {
        return Err("sandbox manager initialization had an invalid version".to_string());
    }

    let mut root_pid = [0; std::mem::size_of::<libc::pid_t>()];
    let mut cleanup_timeout = [0; std::mem::size_of::<u64>()];
    let mut path_length = [0; std::mem::size_of::<u32>()];
    stream
        .read_exact(&mut root_pid)
        .and_then(|()| stream.read_exact(&mut cleanup_timeout))
        .and_then(|()| stream.read_exact(&mut path_length))
        .map_err(|error| format!("failed to read sandbox manager initialization: {error}"))?;
    let root_pid = libc::pid_t::from_be_bytes(root_pid);
    let cleanup_timeout = u64::from_be_bytes(cleanup_timeout);
    let path_length = u32::from_be_bytes(path_length) as usize;
    if root_pid <= 0 || cleanup_timeout == 0 || path_length > MAXIMUM_PATH_BYTES {
        return Err("sandbox manager initialization is invalid".to_string());
    }

    let mut path = vec![0; path_length];
    stream
        .read_exact(&mut path)
        .map_err(|error| format!("failed to read sandbox manager path: {error}"))?;
    Ok((
        root_pid,
        Duration::from_millis(cleanup_timeout),
        PathBuf::from(OsString::from_vec(path)),
    ))
}

fn cleanup_timeout_millis(timeout: Duration) -> Result<u64, String> {
    let milliseconds = timeout
        .as_millis()
        .checked_add(u128::from(
            !timeout.subsec_nanos().is_multiple_of(1_000_000),
        ))
        .and_then(|milliseconds| u64::try_from(milliseconds).ok())
        .filter(|milliseconds| *milliseconds > 0)
        .ok_or_else(|| "sandbox manager cleanup timeout is invalid".to_string())?;
    Ok(milliseconds)
}
