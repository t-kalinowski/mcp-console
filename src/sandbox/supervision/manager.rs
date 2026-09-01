#[path = "manager/entrypoint.rs"]
mod entrypoint;
#[path = "manager/protocol.rs"]
mod protocol;

use super::process::{ProcessIdentity, process_info, signal_process};
use super::process_tracker::ObserverWakeup;
use crate::sandbox::file_descriptors;
use std::io::Read;
use std::net::Shutdown;
use std::os::fd::OwnedFd;
use std::os::unix::net::UnixStream;
use std::os::unix::process::{CommandExt as _, ExitStatusExt as _};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread::JoinHandle;
use std::time::Duration;

const READY: u8 = 1;
const STARTUP_TIMEOUT: Duration = Duration::from_secs(5);
const FINISH_ALLOWANCE: Duration = Duration::from_secs(1);

/// A host process that independently owns one committed sandbox lifetime.
///
/// Normal retirement remains in the owner's continuously serviced observer.
/// Once this manager reports readiness, it supplies a second observation path
/// that survives abrupt loss of the server or standalone launcher. A descendant
/// that escapes before the manager's post-spawn tracker observes it remains
/// outside this guarantee even after readiness.
pub(crate) struct SandboxManager {
    monitor: Option<ManagerMonitor>,
    control: UnixStream,
    cleanup_timeout: Duration,
    retirement_started: bool,
    cleanup_complete: bool,
    control_error: Option<String>,
}

struct ManagerMonitor {
    identity: ProcessIdentity,
    result: Receiver<Result<SandboxManagerExit, String>>,
    thread: Option<JoinHandle<()>>,
}

pub(crate) enum SandboxManagerExit {
    Normal,
    Recovered,
}

#[derive(Clone, Copy, Eq, PartialEq)]
pub(crate) enum CleanupPreparation {
    Complete,
    TimedOut,
    Failed,
}

impl SandboxManager {
    pub(crate) fn start(
        root_pid: u32,
        temporary_directory: &Path,
        cleanup_timeout: Duration,
    ) -> Result<Self, String> {
        Self::start_inner(root_pid, temporary_directory, cleanup_timeout, None)
    }

    pub(super) fn start_for_standalone(
        root_pid: u32,
        temporary_directory: &Path,
        cleanup_timeout: Duration,
        observer_wakeup: ObserverWakeup,
    ) -> Result<Self, String> {
        Self::start_inner(
            root_pid,
            temporary_directory,
            cleanup_timeout,
            Some(observer_wakeup),
        )
    }

    fn start_inner(
        root_pid: u32,
        temporary_directory: &Path,
        cleanup_timeout: Duration,
        observer_wakeup: Option<ObserverWakeup>,
    ) -> Result<Self, String> {
        let owner_pid = libc::pid_t::try_from(std::process::id())
            .ok()
            .filter(|pid| *pid > 0)
            .ok_or_else(|| "sandbox manager owner PID is invalid".to_string())?;
        let root_pid = libc::pid_t::try_from(root_pid)
            .ok()
            .filter(|pid| *pid > 0)
            .ok_or_else(|| "sandbox manager received an invalid root PID".to_string())?;
        let root = process_info(root_pid)?
            .ok_or_else(|| format!("sandbox root {root_pid} exited before manager startup"))?
            .identity;

        let executable = std::env::current_exe()
            .map_err(|error| format!("failed to locate the sandbox manager: {error}"))?;
        let (mut stream, inherited_stream) = UnixStream::pair()
            .map_err(|error| format!("failed to create sandbox manager control: {error}"))?;
        let inherited_input = Stdio::from(OwnedFd::from(inherited_stream));

        let mut command = Command::new(executable);
        command
            .arg("sandbox-manager")
            .stdin(inherited_input)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0);
        // The owner may be multithreaded. Carry the private manager control on
        // fd 0 and apply the same no-extra-descriptor boundary used for relays.
        file_descriptors::close_unlisted_from_multithreaded_parent(&mut command)?;

        let mut child = command
            .spawn()
            .map_err(|error| format!("failed to launch the sandbox manager: {error}"))?;
        // Command retains its configured stdio after spawn. Drop its copy of
        // the manager control so pre-readiness manager exit reaches this read
        // as EOF instead of waiting for the startup deadline.
        drop(command);
        let manager_pid = child.id() as libc::pid_t;
        let identity = match process_info(manager_pid) {
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

        let initialization = (|| {
            stream
                .set_read_timeout(Some(STARTUP_TIMEOUT))
                .and_then(|()| stream.set_write_timeout(Some(STARTUP_TIMEOUT)))
                .map_err(|error| format!("failed to configure sandbox manager control: {error}"))?;
            protocol::write(
                &mut stream,
                owner_pid,
                root_pid,
                cleanup_timeout,
                temporary_directory,
            )?;

            let mut ready = [0];
            stream
                .read_exact(&mut ready)
                .map_err(|error| format!("sandbox manager did not become ready: {error}"))?;
            if ready != [READY] {
                return Err("sandbox manager sent an invalid readiness response".to_string());
            }
            Ok(())
        })();
        if let Err(error) = initialization {
            let _ = stream.shutdown(Shutdown::Both);
            return Err(stop_and_reap(&mut child, error));
        }

        Ok(Self {
            monitor: Some(ManagerMonitor::start(
                child,
                identity,
                root,
                observer_wakeup,
            )),
            control: stream,
            cleanup_timeout,
            retirement_started: false,
            cleanup_complete: false,
            control_error: None,
        })
    }

    /// Marks the beginning of owner-controlled retirement. If the owner exits
    /// before completing the handoff, the manager preserves the directory.
    pub(crate) fn begin_retirement(&mut self) -> bool {
        if self.retirement_started {
            return true;
        }
        if self.control_error.is_some() {
            return false;
        }
        match protocol::write_retirement_started(&mut self.control) {
            Ok(()) => {
                self.retirement_started = true;
                true
            }
            Err(error) => {
                self.control_error = Some(error);
                false
            }
        }
    }

    /// Waits until the manager has retired every identity it observed.
    pub(crate) fn prepare_finish(&mut self) -> CleanupPreparation {
        if self.cleanup_complete {
            return CleanupPreparation::Complete;
        }
        if !self.begin_retirement() {
            return CleanupPreparation::Failed;
        }
        let timeout = self.cleanup_timeout.saturating_add(FINISH_ALLOWANCE);
        let result = self
            .control
            .set_read_timeout(Some(timeout))
            .map_err(|error| format!("failed to configure sandbox manager control: {error}"))
            .and_then(|()| protocol::read_cleanup_complete(&mut self.control));
        match result {
            Ok(protocol::CleanupAcknowledgement::Complete) => {
                self.cleanup_complete = true;
                CleanupPreparation::Complete
            }
            Ok(protocol::CleanupAcknowledgement::TimedOut) => {
                // Discovery and the first complete signal pass precede the
                // manager's bounded cleanup grace. Preserve the directory, then
                // let the bounded manager monitor prove whether cleanup finished.
                CleanupPreparation::TimedOut
            }
            Err(error) => {
                self.control_error = Some(error);
                CleanupPreparation::Failed
            }
        }
    }

    /// Commits the owner's final directory disposition and waits for the
    /// manager process to exit.
    pub(crate) fn finish(
        mut self,
        preserve_temporary_directory: bool,
    ) -> Result<SandboxManagerExit, String> {
        if let Err(error) =
            protocol::write_retirement_disposition(&mut self.control, preserve_temporary_directory)
            && self.control_error.is_none()
        {
            self.control_error = Some(error);
        }
        let _ = self.control.shutdown(Shutdown::Both);
        let timeout = self.cleanup_timeout.saturating_add(FINISH_ALLOWANCE);
        let monitor = self
            .monitor
            .take()
            .expect("active sandbox manager should retain its monitor")
            .finish(timeout);
        match monitor {
            Ok(SandboxManagerExit::Recovered) => Ok(SandboxManagerExit::Recovered),
            Ok(SandboxManagerExit::Normal) => self
                .control_error
                .map_or(Ok(SandboxManagerExit::Normal), Err),
            Err(monitor_error) => Err(with_prior_error(self.control_error, monitor_error)),
        }
    }
}

impl ManagerMonitor {
    fn start(
        mut child: Child,
        identity: ProcessIdentity,
        root: ProcessIdentity,
        observer_wakeup: Option<ObserverWakeup>,
    ) -> Self {
        let (result_sender, result) = mpsc::channel();
        let thread = std::thread::spawn(move || {
            let result = match child.wait() {
                Ok(status) if status.success() => Ok(SandboxManagerExit::Normal),
                Ok(status) if status.signal().is_some() => recover_after_manager_signal(
                    format!("sandbox manager exited with status {status}"),
                    root,
                ),
                Ok(status) => fail_after_manager_error(
                    format!("sandbox manager exited with status {status}"),
                    root,
                ),
                Err(error) => fail_after_manager_error(
                    format!("failed to wait for sandbox manager: {error}"),
                    root,
                ),
            };
            let _ = result_sender.send(result);
            if let Some(observer_wakeup) = observer_wakeup {
                let _ = observer_wakeup.wake();
            }
        });
        Self {
            identity,
            result,
            thread: Some(thread),
        }
    }

    fn finish(mut self, timeout: Duration) -> Result<SandboxManagerExit, String> {
        let mut error = None;
        let result = match self.result.recv_timeout(timeout) {
            Ok(result) => Some(result),
            Err(RecvTimeoutError::Timeout) => {
                error = Some("timed out waiting for sandbox manager cleanup".to_string());
                if let Err(signal_error) = signal_process(self.identity, libc::SIGKILL) {
                    error = Some(with_prior_error(
                        error,
                        format!("failed to stop sandbox manager: {signal_error}"),
                    ));
                }
                match self.result.recv_timeout(FINISH_ALLOWANCE) {
                    Ok(result) => Some(result),
                    Err(RecvTimeoutError::Disconnected) => Some(Err(
                        "sandbox manager monitor ended without a result".to_string(),
                    )),
                    Err(RecvTimeoutError::Timeout) => {
                        error = Some(with_prior_error(
                            error,
                            "sandbox manager did not stop after forced termination".to_string(),
                        ));
                        None
                    }
                }
            }
            Err(RecvTimeoutError::Disconnected) => Some(Err(
                "sandbox manager monitor ended without a result".to_string(),
            )),
        };
        let mut exit = None;
        let can_join = match result {
            Some(Ok(manager_exit)) => {
                exit = Some(manager_exit);
                true
            }
            Some(Err(result_error)) => {
                error = Some(with_prior_error(error, result_error));
                true
            }
            None => false,
        };
        if can_join
            && self
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

fn recover_after_manager_signal(
    manager_error: String,
    root: ProcessIdentity,
) -> Result<SandboxManagerExit, String> {
    match signal_process(root, libc::SIGKILL) {
        Ok(_) => Ok(SandboxManagerExit::Recovered),
        Err(recovery_error) => Err(format!(
            "{manager_error}; additionally, manager recovery failed: {recovery_error}"
        )),
    }
}

fn fail_after_manager_error(
    manager_error: String,
    root: ProcessIdentity,
) -> Result<SandboxManagerExit, String> {
    match signal_process(root, libc::SIGKILL) {
        Ok(_) => Err(manager_error),
        Err(recovery_error) => Err(format!(
            "{manager_error}; additionally, manager recovery failed: {recovery_error}"
        )),
    }
}

pub(super) fn run() -> Result<(), String> {
    entrypoint::run()
}

pub(super) fn with_prior_error(prior: Option<String>, error: String) -> String {
    prior.map_or(error.clone(), |prior| {
        format!("{prior}; additionally, {error}")
    })
}

fn stop_and_reap(child: &mut Child, mut error: String) -> String {
    if let Err(kill_error) = child.kill()
        && kill_error.raw_os_error() != Some(libc::ESRCH)
    {
        error = with_prior_error(
            Some(error),
            format!("failed to stop sandbox manager: {kill_error}"),
        );
    }
    if let Err(wait_error) = child.wait() {
        error = with_prior_error(
            Some(error),
            format!("failed to reap sandbox manager: {wait_error}"),
        );
    }
    error
}
