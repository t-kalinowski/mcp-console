#[path = "manager/entrypoint.rs"]
mod entrypoint;

use super::job_control::SignalRelay;
use super::process::{ProcessIdentity, process_info, signal_process};
use super::process_tracker::DescendantTracker;
use super::root_exit_waiter::RootExitWakeup;
use crate::sandbox::file_descriptors;
use crate::sandbox::platform;
use std::io::Read;
use std::os::fd::OwnedFd;
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt as _;
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread::JoinHandle;
use std::time::Duration;

const STARTUP_TIMEOUT: Duration = Duration::from_secs(5);
const EXIT_ALLOWANCE: Duration = Duration::from_secs(1);
pub(super) const READY: u8 = 1;

pub(crate) struct SandboxManager {
    handle: Option<ManagerHandle>,
}

struct ManagerHandle {
    monitor: ManagerMonitor,
    stream: UnixStream,
    cleanup_timeout: Duration,
}

struct ManagerMonitor {
    identity: ProcessIdentity,
    recovery_enabled: Arc<AtomicBool>,
    result: Receiver<Result<(), String>>,
    thread: Option<JoinHandle<()>>,
}

struct ManagerMonitorStartError {
    child: Child,
    message: String,
}

impl SandboxManager {
    /// Starts supervision for an existing gated root and returns only after
    /// readiness and owner-side manager-failure monitoring are established.
    pub(in crate::sandbox) fn start(
        root_pid: u32,
        temporary_directory: &mut platform::TemporaryDirectory,
        cleanup_timeout: Duration,
        signal_relay: Option<&SignalRelay>,
        root_wakeup: Option<RootExitWakeup>,
    ) -> Result<Self, String> {
        let root_pid_value = libc::pid_t::try_from(root_pid)
            .ok()
            .filter(|pid| *pid > 0)
            .ok_or_else(|| "sandbox manager received an invalid root PID".to_string())?;
        let cleanup_timeout_millis = u64::try_from(cleanup_timeout.as_millis())
            .ok()
            .filter(|milliseconds| *milliseconds > 0)
            .ok_or_else(|| "sandbox manager cleanup timeout is invalid".to_string())?;
        let executable = std::env::current_exe()
            .map_err(|error| format!("failed to locate the sandbox manager: {error}"))?;
        let (stream, inherited_stream) = UnixStream::pair()
            .map_err(|error| format!("failed to create sandbox manager control: {error}"))?;
        let inherited_input = Stdio::from(OwnedFd::from(inherited_stream));

        let mut command = Command::new(executable);
        command
            .arg("sandbox-manager")
            .arg("--root-pid")
            .arg(root_pid.to_string())
            .arg("--cleanup-timeout-millis")
            .arg(cleanup_timeout_millis.to_string())
            .arg("--temporary-directory")
            .arg(temporary_directory.path())
            .stdin(inherited_input)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0);
        if let Some(signal_relay) = signal_relay {
            signal_relay.configure_manager(&mut command);
        }
        file_descriptors::close_unlisted_from_multithreaded_parent(&mut command)?;

        let mut child = command
            .spawn()
            .map_err(|error| format!("failed to launch the sandbox manager: {error}"))?;
        // Command retains its configured stdio after spawn. Drop its copy of
        // the manager control so pre-readiness manager exit reaches the
        // readiness wait as EOF instead of waiting for the startup deadline.
        drop(command);
        let child_pid = child.id() as libc::pid_t;
        let child_identity = match process_info(child_pid) {
            Ok(Some(info)) if !info.is_zombie => info.identity,
            Ok(_) => {
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
        let mut stream = stream;
        if let Err(error) = stream.set_read_timeout(Some(STARTUP_TIMEOUT)) {
            drop(stream);
            return Err(stop_and_reap(
                &mut child,
                format!("failed to configure sandbox manager control: {error}"),
            ));
        }

        let mut ready = [0];
        if let Err(error) = stream.read_exact(&mut ready) {
            drop(stream);
            return Err(stop_and_reap(
                &mut child,
                format!("sandbox manager did not become ready: {error}"),
            ));
        }
        if ready != [READY] {
            drop(stream);
            return Err(stop_and_reap(
                &mut child,
                "sandbox manager sent an invalid readiness response".to_string(),
            ));
        }
        let monitor = match ManagerMonitor::start(
            child,
            child_identity,
            root_pid_value,
            cleanup_timeout,
            root_wakeup,
        ) {
            Ok(monitor) => monitor,
            Err(mut error) => {
                return Err(stop_and_reap_before_ownership_release(
                    &mut error.child,
                    stream,
                    error.message,
                ));
            }
        };
        temporary_directory.relinquish();
        Ok(Self {
            handle: Some(ManagerHandle {
                monitor,
                stream,
                cleanup_timeout,
            }),
        })
    }

    /// Closes the ownership token and waits for the manager to retire the
    /// sandbox lifetime. The manager removes the private directory only after
    /// it has completed process cleanup successfully.
    pub(crate) fn retire(&mut self) -> Result<(), String> {
        self.wait_for_exit()
    }

    fn wait_for_exit(&mut self) -> Result<(), String> {
        let Some(handle) = self.handle.take() else {
            return Ok(());
        };
        drop(handle.stream);
        let finish_timeout = handle.cleanup_timeout.saturating_add(EXIT_ALLOWANCE);
        handle.monitor.finish(finish_timeout)
    }
}

impl ManagerMonitor {
    fn start(
        child: Child,
        identity: ProcessIdentity,
        root_pid: libc::pid_t,
        cleanup_timeout: Duration,
        root_wakeup: Option<RootExitWakeup>,
    ) -> Result<Self, ManagerMonitorStartError> {
        let (result_sender, result) = mpsc::channel();
        let (child_sender, child_receiver) = mpsc::sync_channel(0);
        let recovery_enabled = Arc::new(AtomicBool::new(true));
        let recovery_enabled_for_monitor = Arc::clone(&recovery_enabled);
        let thread = match std::thread::Builder::new().spawn(move || {
            let child = child_receiver
                .recv()
                .expect("sandbox manager child should be sent to its monitor");
            let _wake_root = root_wakeup.map(RootExitWakeup::on_drop);
            let result = monitor_manager(
                child,
                root_pid,
                cleanup_timeout,
                recovery_enabled_for_monitor,
            );
            let _ = result_sender.send(result);
        }) {
            Ok(thread) => thread,
            Err(error) => {
                return Err(ManagerMonitorStartError {
                    child,
                    message: format!("failed to start sandbox manager monitor: {error}"),
                });
            }
        };
        if let Err(error) = child_sender.send(child) {
            let _ = thread.join();
            return Err(ManagerMonitorStartError {
                child: error.0,
                message: "sandbox manager monitor ended during startup".to_string(),
            });
        }
        Ok(Self {
            identity,
            recovery_enabled,
            result,
            thread: Some(thread),
        })
    }

    fn disable_recovery(&self) -> bool {
        self.recovery_enabled.swap(false, Ordering::SeqCst)
    }

    fn finish(mut self, timeout: Duration) -> Result<(), String> {
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
                // thread still owns the exact, unreaped manager child. Allow a
                // second cleanup deadline for forced-exit recovery, then detach
                // the monitor rather than extending the bounded retirement wait.
                // If fallback recovery has already started, retain the root pin
                // until that bounded cleanup finishes before detaching.
                match self.result.recv_timeout(timeout) {
                    Ok(result) => result,
                    Err(RecvTimeoutError::Disconnected) => {
                        Err("sandbox manager monitor ended without a result".to_string())
                    }
                    Err(RecvTimeoutError::Timeout) => {
                        if !self.disable_recovery() {
                            // Fallback recovery claimed the root before
                            // cancellation. Retain its PID pin until that
                            // bounded cleanup finishes.
                            let _ = self
                                .thread
                                .take()
                                .expect("sandbox manager monitor thread should be joinable")
                                .join();
                        }
                        return Err(with_prior_error(
                            error,
                            "sandbox manager did not stop after forced termination".to_string(),
                        ));
                    }
                }
            }
            Err(RecvTimeoutError::Disconnected) => {
                Err("sandbox manager monitor ended without a result".to_string())
            }
        };
        match result {
            Ok(()) => {}
            Err(result_error) => {
                error = Some(with_prior_error(error, result_error));
            }
        }
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
        error.map_or(Ok(()), Err)
    }
}

fn monitor_manager(
    mut child: Child,
    root_pid: libc::pid_t,
    cleanup_timeout: Duration,
    recovery_enabled: Arc<AtomicBool>,
) -> Result<(), String> {
    let completion = child
        .wait()
        .map_err(|wait_error| format!("failed to reap sandbox manager: {wait_error}"));

    match completion {
        Ok(status) if status.success() => Ok(()),
        Ok(status) => finish_manager_failure(
            format!("sandbox manager exited with status {status}"),
            root_pid,
            cleanup_timeout,
            &recovery_enabled,
        ),
        Err(error) => finish_manager_failure(error, root_pid, cleanup_timeout, &recovery_enabled),
    }
}

fn finish_manager_failure(
    mut error: String,
    root_pid: libc::pid_t,
    cleanup_timeout: Duration,
    recovery_enabled: &AtomicBool,
) -> Result<(), String> {
    if !recovery_enabled.swap(false, Ordering::SeqCst) {
        return Err(error);
    }
    let root = match process_info(root_pid) {
        Ok(Some(info)) if !info.is_zombie => info.identity,
        Ok(info) => {
            error = with_prior_error(
                Some(error),
                format!(
                    "manager recovery failed: sandbox root {root_pid} exited before fallback supervision"
                ),
            );
            if info.is_some()
                && let Err(group_error) = stop_pinned_process_group(root_pid)
            {
                error = with_prior_error(
                    Some(error),
                    format!("manager recovery failed: {group_error}"),
                );
            }
            return Err(error);
        }
        Err(inspect_error) => {
            error = with_prior_error(
                Some(error),
                format!("manager recovery failed: {inspect_error}"),
            );
            if let Err(group_error) = stop_pinned_process_group(root_pid) {
                error = with_prior_error(
                    Some(error),
                    format!("manager recovery failed: {group_error}"),
                );
            }
            return Err(error);
        }
    };
    let cleanup_error = match DescendantTracker::start(root_pid) {
        Ok(tracker) => tracker.stop(cleanup_timeout).err(),
        Err(failure) => Some(failure.retire(cleanup_timeout)),
    };
    let group_error = stop_process_group(root).err();
    if cleanup_error.is_none() && group_error.is_none() {
        return Ok(());
    }
    if let Some(cleanup_error) = cleanup_error {
        error = with_prior_error(
            Some(error),
            format!("manager recovery failed: {cleanup_error}"),
        );
        if let Err(signal_error) = signal_process(root, libc::SIGKILL) {
            error = with_prior_error(Some(error), signal_error);
        }
    }
    if let Some(group_error) = group_error {
        error = with_prior_error(
            Some(error),
            format!("manager recovery failed: {group_error}"),
        );
    }
    Err(error)
}

fn stop_process_group(root: ProcessIdentity) -> Result<(), String> {
    let info = process_info(root.pid)?
        .ok_or_else(|| format!("sandbox root {} exited before group cleanup", root.pid))?;
    if info.identity != root {
        return Err(format!(
            "sandbox root {} changed before group cleanup",
            root.pid
        ));
    }
    stop_pinned_process_group(root.pid)
}

fn stop_pinned_process_group(root_pid: libc::pid_t) -> Result<(), String> {
    // The owner keeps the direct root waitable until monitor recovery returns,
    // so its PID and process-group identity remain pinned even when libproc
    // cannot inspect it.
    let root_process_id =
        u32::try_from(root_pid).map_err(|_| format!("invalid sandbox process group {root_pid}"))?;
    let Err(group_error) = platform::kill_process_group(root_process_id) else {
        return Ok(());
    };

    let mut error = format!("failed to stop sandbox process group: {group_error}");
    if unsafe { libc::kill(root_pid, libc::SIGKILL) } < 0 {
        let root_error = std::io::Error::last_os_error();
        if root_error.raw_os_error() != Some(libc::ESRCH) {
            error = with_prior_error(
                Some(error),
                format!("failed to stop pinned sandbox root {root_pid}: {root_error}"),
            );
        }
    }
    Err(error)
}

fn with_prior_error(prior: Option<String>, error: String) -> String {
    prior.map_or(error.clone(), |prior| {
        format!("{prior}; additionally, {error}")
    })
}

fn stop_and_reap(child: &mut Child, mut error: String) -> String {
    signal_manager_stop(child, &mut error);
    reap_manager(child, error)
}

fn stop_and_reap_before_ownership_release(
    child: &mut Child,
    stream: UnixStream,
    mut error: String,
) -> String {
    if !signal_manager_stop(child, &mut error) {
        // EOF is the only bounded route to manager exit when signaling fails.
        drop(stream);
        return reap_manager(child, error);
    }
    let error = reap_manager(child, error);
    drop(stream);
    error
}

fn signal_manager_stop(child: &mut Child, error: &mut String) -> bool {
    if let Err(kill_error) = child.kill()
        && kill_error.raw_os_error() != Some(libc::ESRCH)
    {
        error.push_str(&format!(
            "; additionally, failed to stop manager: {kill_error}"
        ));
        return false;
    }
    true
}

fn reap_manager(child: &mut Child, mut error: String) -> String {
    if let Err(wait_error) = child.wait() {
        error.push_str(&format!(
            "; additionally, failed to reap manager: {wait_error}"
        ));
    }
    error
}

impl Drop for SandboxManager {
    fn drop(&mut self) {
        let _ = self.wait_for_exit();
    }
}

pub(super) fn run(
    root_pid: u32,
    cleanup_timeout_millis: u64,
    temporary_directory: std::path::PathBuf,
) -> Result<(), String> {
    entrypoint::run(root_pid, cleanup_timeout_millis, temporary_directory)
}
