#[path = "manager/entrypoint.rs"]
mod entrypoint;
#[path = "manager/protocol.rs"]
mod protocol;

use super::process::{ProcessIdentity, process_info, signal_process};
use crate::sandbox::{file_descriptors, platform};
use std::io::Read;
use std::net::Shutdown;
use std::os::fd::OwnedFd;
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt as _;
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
/// Normal retirement remains in the server's continuously serviced observer.
/// Once this manager reports readiness, it supplies a second observation path
/// that survives abrupt loss of the server process. The interval between the
/// relay spawn and manager readiness remains outside this guarantee.
pub(crate) struct SandboxManager {
    monitor: Option<ManagerMonitor>,
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
    pub(crate) fn start(
        root_pid: u32,
        temporary_directory: &Path,
        separate_process_group: bool,
        cleanup_timeout: Duration,
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
        // The server is multithreaded. Carry the private manager control on fd 0
        // and apply the same no-extra-descriptor boundary used for worker relays.
        file_descriptors::close_unlisted_from_multithreaded_parent(&mut command)?;

        let mut child = command
            .spawn()
            .map_err(|error| format!("failed to launch the sandbox manager: {error}"))?;
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
                separate_process_group,
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
        let _ = stream.shutdown(Shutdown::Both);
        drop(stream);
        if let Err(error) = initialization {
            return Err(stop_and_reap(&mut child, error));
        }

        Ok(Self {
            monitor: Some(ManagerMonitor::start(
                child,
                identity,
                root,
                separate_process_group,
            )),
            cleanup_timeout,
        })
    }

    /// Waits for the crash manager after normal in-process retirement has
    /// stopped the sandbox root and its observed descendants.
    pub(crate) fn finish(mut self) -> Result<(), String> {
        let timeout = self.cleanup_timeout.saturating_add(FINISH_ALLOWANCE);
        self.monitor
            .take()
            .expect("active sandbox manager should retain its monitor")
            .finish(timeout)
            .map(|_| ())
    }
}

impl ManagerMonitor {
    fn start(
        mut child: Child,
        identity: ProcessIdentity,
        root: ProcessIdentity,
        separate_process_group: bool,
    ) -> Self {
        let (result_sender, result) = mpsc::channel();
        let thread = std::thread::spawn(move || {
            let result = match child.wait() {
                Ok(status) if status.success() => Ok(ManagerExit::Normal),
                Ok(status) => recover_after_manager_failure(
                    format!("sandbox manager exited with status {status}"),
                    root,
                    separate_process_group,
                ),
                Err(error) => recover_after_manager_failure(
                    format!("failed to wait for sandbox manager: {error}"),
                    root,
                    separate_process_group,
                ),
            };
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

fn recover_after_manager_failure(
    manager_error: String,
    root: ProcessIdentity,
    separate_process_group: bool,
) -> Result<ManagerExit, String> {
    let mut recovery_error = None;
    if let Err(error) = signal_process(root, libc::SIGKILL) {
        recovery_error = Some(error);
    }
    if separate_process_group
        && let Err(error) = platform::kill_process_group(root.pid as u32)
    {
        recovery_error = Some(with_prior_error(
            recovery_error,
            format!("failed to stop sandbox process group: {error}"),
        ));
    }
    match recovery_error {
        None => Ok(ManagerExit::Recovered),
        Some(recovery_error) => Err(format!(
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
