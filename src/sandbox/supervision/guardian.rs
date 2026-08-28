use super::file_descriptors::configure as configure_file_descriptors;
use super::process::{process_info, signal_process};
use super::process_tracker::DescendantTracker;
use crate::sandbox::platform;
use std::ffi::OsString;
use std::io::{ErrorKind, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt as _;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

const CONTROL_DESCRIPTOR_ENV: &str = "MCP_CONSOLE_SANDBOX_GUARDIAN_FD";
const INITIALIZATION_MAGIC: &[u8; 4] = b"MCG1";
const READY: u8 = 1;
const COMMIT: u8 = 2;
const FINISH_REMOVE: u8 = 3;
const FINISH_PRESERVE: u8 = 4;
const MAXIMUM_PATH_BYTES: usize = 16 * 1024;
const STARTUP_TIMEOUT: Duration = Duration::from_secs(5);
const CLEANUP_TIMEOUT: Duration = Duration::from_secs(5);
const FINISH_TIMEOUT: Duration = Duration::from_secs(6);

pub(super) struct Guardian {
    child: Option<Child>,
    stream: Option<UnixStream>,
}

impl Guardian {
    pub(super) fn spawn() -> Result<Self, String> {
        let executable = std::env::current_exe()
            .map_err(|error| format!("failed to locate the sandbox guardian: {error}"))?;
        let (stream, inherited_stream) = UnixStream::pair()
            .map_err(|error| format!("failed to create sandbox guardian control: {error}"))?;
        let inherited_descriptor = inherited_stream.as_raw_fd();

        let mut command = Command::new(executable);
        command
            .arg("sandbox-guardian")
            .env(CONTROL_DESCRIPTOR_ENV, inherited_descriptor.to_string())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0);
        configure_file_descriptors(&mut command, vec![inherited_descriptor])?;

        let child = command
            .spawn()
            .map_err(|error| format!("failed to launch the sandbox guardian: {error}"))?;
        drop(inherited_stream);
        Ok(Self {
            child: Some(child),
            stream: Some(stream),
        })
    }

    pub(super) fn observe(
        &mut self,
        root_pid: u32,
        temporary_directory: &Path,
    ) -> Result<(), String> {
        let root_pid = libc::pid_t::try_from(root_pid)
            .ok()
            .filter(|pid| *pid > 0)
            .ok_or_else(|| "sandbox guardian received an invalid root PID".to_string())?;
        let path = temporary_directory.as_os_str().as_bytes();
        let path_length = u32::try_from(path.len())
            .ok()
            .filter(|length| *length as usize <= MAXIMUM_PATH_BYTES)
            .ok_or_else(|| "sandbox guardian temporary path is too long".to_string())?;
        let stream = self
            .stream
            .as_mut()
            .expect("sandbox guardian control should be available");
        stream
            .set_read_timeout(Some(STARTUP_TIMEOUT))
            .map_err(|error| format!("failed to configure sandbox guardian control: {error}"))?;
        stream
            .set_write_timeout(Some(STARTUP_TIMEOUT))
            .map_err(|error| format!("failed to configure sandbox guardian control: {error}"))?;

        stream
            .write_all(INITIALIZATION_MAGIC)
            .and_then(|()| stream.write_all(&root_pid.to_be_bytes()))
            .and_then(|()| stream.write_all(&path_length.to_be_bytes()))
            .and_then(|()| stream.write_all(path))
            .map_err(|error| format!("failed to initialize sandbox guardian: {error}"))?;

        let mut ready = [0];
        stream
            .read_exact(&mut ready)
            .map_err(|error| format!("sandbox guardian did not become ready: {error}"))?;
        if ready != [READY] {
            return Err("sandbox guardian sent an invalid readiness response".to_string());
        }
        Ok(())
    }

    pub(super) fn commit(&mut self) -> Result<(), String> {
        self.stream
            .as_mut()
            .expect("sandbox guardian control should be available")
            .write_all(&[COMMIT])
            .map_err(|error| format!("failed to commit sandbox guardian ownership: {error}"))
    }

    pub(super) fn finish(mut self, preserve_temporary_directory: bool) -> Result<(), String> {
        self.finish_inner(Some(preserve_temporary_directory), true)
    }

    fn finish_inner(
        &mut self,
        preserve_temporary_directory: Option<bool>,
        inspect_status: bool,
    ) -> Result<(), String> {
        let mut error = None;
        if let Some(mut stream) = self.stream.take()
            && let Some(preserve_temporary_directory) = preserve_temporary_directory
        {
            let disposition = if preserve_temporary_directory {
                FINISH_PRESERVE
            } else {
                FINISH_REMOVE
            };
            if let Err(write_error) = stream.write_all(&[disposition]) {
                error = Some(format!(
                    "failed to finish sandbox guardian ownership: {write_error}"
                ));
            }
        }
        let Some(mut child) = self.child.take() else {
            return error.map_or(Ok(()), Err);
        };
        let exited =
            match platform::wait_for_process_exit_without_reaping(child.id(), FINISH_TIMEOUT) {
                Ok(exited) => exited,
                Err(wait_error) => {
                    let wait_error = stop_and_reap(
                        &mut child,
                        format!("failed to wait for sandbox guardian: {wait_error}"),
                    );
                    return Err(with_prior_error(error, wait_error));
                }
            };
        if !exited {
            let timeout = stop_and_reap(
                &mut child,
                "timed out waiting for sandbox guardian cleanup".to_string(),
            );
            return Err(with_prior_error(error, timeout));
        }
        let status = match child.wait() {
            Ok(status) => status,
            Err(wait_error) => {
                let wait_error = format!("failed to reap sandbox guardian: {wait_error}");
                return Err(with_prior_error(error, wait_error));
            }
        };
        if inspect_status && !status.success() {
            let status_error = format!("sandbox guardian exited with status {status}");
            return Err(with_prior_error(error, status_error));
        }
        error.map_or(Ok(()), Err)
    }
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
            "; additionally, failed to stop guardian: {kill_error}"
        ));
    }
    if let Err(wait_error) = child.wait() {
        error.push_str(&format!(
            "; additionally, failed to reap guardian: {wait_error}"
        ));
    }
    error
}

impl Drop for Guardian {
    fn drop(&mut self) {
        let _ = self.finish_inner(None, false);
    }
}

pub(super) fn run() -> Result<(), String> {
    // SAFETY: getppid(2) has no pointer or lifetime preconditions.
    let owner_pid = unsafe { libc::getppid() };
    if owner_pid <= 0 {
        return Err("sandbox guardian has no valid owner".to_string());
    }

    let mut stream = inherited_control()?;
    let (root_pid, temporary_directory) = read_initialization(&mut stream)?;
    let info = process_info(root_pid)?
        .ok_or_else(|| format!("sandbox root {root_pid} exited before guardian startup"))?;
    if info.parent_pid != owner_pid {
        return Err(format!(
            "sandbox root {root_pid} is not a child of guardian owner {owner_pid}"
        ));
    }

    let tracker = DescendantTracker::start(root_pid)?;
    let temporary_directory = platform::TemporaryDirectory::adopt(temporary_directory, owner_pid)?;
    let root = info.identity;

    if let Err(error) = stream.write_all(&[READY]) {
        return finish_startup_failure(
            format!("failed to report sandbox guardian readiness: {error}"),
            root,
            tracker,
            temporary_directory,
        );
    }

    let mut commit = [0];
    if let Err(error) = stream.read_exact(&mut commit) {
        return finish_startup_failure(
            format!("sandbox guardian ownership was not committed: {error}"),
            root,
            tracker,
            temporary_directory,
        );
    }
    if commit != [COMMIT] {
        return finish_startup_failure(
            "sandbox guardian ownership commit is invalid".to_string(),
            root,
            tracker,
            temporary_directory,
        );
    }

    let tracker_thread = std::thread::spawn(move || tracker.supervise(CLEANUP_TIMEOUT));
    let disposition = read_owner_disposition(&mut stream, root);
    let tracker_result = tracker_thread
        .join()
        .map_err(|_| "sandbox guardian process tracker failed".to_string())
        .and_then(|result| result);

    let mut error = disposition.error;
    if let Err(tracker_error) = tracker_result {
        error = Some(with_prior_error(error, tracker_error));
    }
    if disposition.preserve || error.is_some() {
        temporary_directory.preserve();
    }
    error.map_or(Ok(()), Err)
}

fn finish_startup_failure(
    mut error: String,
    root: super::process::ProcessIdentity,
    tracker: DescendantTracker,
    temporary_directory: platform::TemporaryDirectory,
) -> Result<(), String> {
    if let Err(signal_error) = signal_process(root, libc::SIGKILL) {
        error.push_str(&format!("; additionally, {signal_error}"));
    }
    if let Err(cleanup_error) = tracker.supervise(CLEANUP_TIMEOUT) {
        error.push_str(&format!("; additionally, {cleanup_error}"));
    }
    temporary_directory.preserve();
    Err(error)
}

struct OwnerDisposition {
    preserve: bool,
    error: Option<String>,
}

fn read_owner_disposition(
    stream: &mut UnixStream,
    root: super::process::ProcessIdentity,
) -> OwnerDisposition {
    let mut disposition = [0];
    loop {
        match stream.read(&mut disposition) {
            Ok(0) => {
                let error = signal_process(root, libc::SIGKILL).err();
                return OwnerDisposition {
                    preserve: error.is_some(),
                    error,
                };
            }
            Ok(_) if disposition == [FINISH_REMOVE] => {
                return OwnerDisposition {
                    preserve: false,
                    error: None,
                };
            }
            Ok(_) if disposition == [FINISH_PRESERVE] => {
                return OwnerDisposition {
                    preserve: true,
                    error: None,
                };
            }
            Ok(_) => {
                let mut error = "sandbox guardian received an invalid finish request".to_string();
                if let Err(signal_error) = signal_process(root, libc::SIGKILL) {
                    error.push_str(&format!("; additionally, {signal_error}"));
                }
                return OwnerDisposition {
                    preserve: true,
                    error: Some(error),
                };
            }
            Err(error) if error.kind() == ErrorKind::Interrupted => {}
            Err(read_error) => {
                let mut error = format!("sandbox guardian control failed: {read_error}");
                if let Err(signal_error) = signal_process(root, libc::SIGKILL) {
                    error.push_str(&format!("; additionally, {signal_error}"));
                }
                return OwnerDisposition {
                    preserve: true,
                    error: Some(error),
                };
            }
        }
    }
}

fn inherited_control() -> Result<UnixStream, String> {
    let descriptor = std::env::var(CONTROL_DESCRIPTOR_ENV)
        .map_err(|_| "sandbox guardian control descriptor is missing".to_string())?
        .parse::<libc::c_int>()
        .ok()
        .filter(|descriptor| *descriptor > libc::STDERR_FILENO)
        .ok_or_else(|| "sandbox guardian control descriptor is invalid".to_string())?;
    Ok(unsafe { UnixStream::from_raw_fd(descriptor) })
}

fn read_initialization(stream: &mut UnixStream) -> Result<(libc::pid_t, PathBuf), String> {
    let mut magic = [0; INITIALIZATION_MAGIC.len()];
    stream
        .read_exact(&mut magic)
        .map_err(|error| format!("failed to read sandbox guardian initialization: {error}"))?;
    if &magic != INITIALIZATION_MAGIC {
        return Err("sandbox guardian initialization had an invalid version".to_string());
    }

    let mut root_pid = [0; std::mem::size_of::<libc::pid_t>()];
    let mut path_length = [0; std::mem::size_of::<u32>()];
    stream
        .read_exact(&mut root_pid)
        .and_then(|()| stream.read_exact(&mut path_length))
        .map_err(|error| format!("failed to read sandbox guardian initialization: {error}"))?;
    let root_pid = libc::pid_t::from_be_bytes(root_pid);
    let path_length = u32::from_be_bytes(path_length) as usize;
    if root_pid <= 0 || path_length > MAXIMUM_PATH_BYTES {
        return Err("sandbox guardian initialization is invalid".to_string());
    }

    let mut path = vec![0; path_length];
    stream
        .read_exact(&mut path)
        .map_err(|error| format!("failed to read sandbox guardian path: {error}"))?;
    Ok((root_pid, PathBuf::from(OsString::from_vec(path))))
}
