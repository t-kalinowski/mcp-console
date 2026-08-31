#[path = "manager/entrypoint.rs"]
mod entrypoint;
#[path = "manager/protocol.rs"]
mod protocol;

use super::process::{ProcessIdentity, process_info, signal_process};
use super::process_tracker::DescendantTracker;
use crate::sandbox::file_descriptors::configure as configure_file_descriptors;
use crate::sandbox::platform;
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt as _;
use std::path::Path;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread::JoinHandle;
use std::time::Duration;

const CONTROL_DESCRIPTOR_ENV: &str = "MCP_CONSOLE_SANDBOX_MANAGER_FD";
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
        let _ = protocol::cleanup_timeout_millis(cleanup_timeout)?;
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
        let owner_pid = libc::pid_t::try_from(std::process::id())
            .ok()
            .filter(|pid| *pid > 0)
            .ok_or_else(|| "sandbox manager owner PID is invalid".to_string())?;
        let root_pid = libc::pid_t::try_from(root_pid)
            .ok()
            .filter(|pid| *pid > 0)
            .ok_or_else(|| "sandbox manager received an invalid root PID".to_string())?;
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

        protocol::write(
            stream,
            owner_pid,
            root_pid,
            self.cleanup_timeout,
            temporary_directory,
        )?;

        let mut ready = [0];
        stream
            .read_exact(&mut ready)
            .map_err(|error| format!("sandbox manager did not become ready: {error}"))?;
        if ready != [protocol::READY] {
            return Err("sandbox manager sent an invalid readiness response".to_string());
        }
        Ok(())
    }

    /// Watches a ready manager and takes over cleanup only if it exits
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
            .expect("ready sandbox manager should remain waitable");
        let child_identity = self
            .child_identity
            .take()
            .expect("ready sandbox manager identity should remain pinned");
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
            .write_all(&[protocol::COMMIT])
            .map_err(|error| format!("failed to commit sandbox manager ownership: {error}"))?;
        let mut committed = [0];
        stream
            .read_exact(&mut committed)
            .map_err(|error| format!("sandbox manager did not confirm ownership: {error}"))?;
        if committed != [protocol::COMMITTED] {
            return Err("sandbox manager sent an invalid ownership confirmation".to_string());
        }
        Ok(())
    }

    /// Completes ownership after the sandbox root has already exited.
    pub(crate) fn finish(mut self) -> Result<(), String> {
        self.finish_inner(Some(protocol::FINISH), true)
    }

    /// Stops the recorded sandbox root before completing ownership.
    pub(crate) fn stop(mut self) -> Result<(), String> {
        self.finish_inner(Some(protocol::STOP), true)
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
    let cleanup_error = match DescendantTracker::start(root_pid) {
        Ok(tracker) => tracker.stop(cleanup_timeout).err(),
        Err(failure) => Some(failure.retire(cleanup_timeout)),
    };
    let group_error = stop_process_group(root).err();
    if cleanup_error.is_none() && group_error.is_none() {
        return Ok(ManagerExit::Recovered);
    }
    if let Some(cleanup_error) = cleanup_error {
        error = with_prior_error(Some(error), cleanup_error);
        if let Err(signal_error) = signal_process(root, libc::SIGKILL) {
            error = with_prior_error(Some(error), signal_error);
        }
    }
    if let Some(group_error) = group_error {
        error = with_prior_error(Some(error), group_error);
    }
    temporary_directory.preserve();
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
    let root_pid = u32::try_from(root.pid)
        .map_err(|_| format!("invalid sandbox process group {}", root.pid))?;
    platform::kill_process_group(root_pid)
        .map_err(|error| format!("failed to stop sandbox process group: {error}"))
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
    entrypoint::run()
}
