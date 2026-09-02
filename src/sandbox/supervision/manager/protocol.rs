use std::ffi::OsString;
use std::io::{ErrorKind, Read, Write};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};
use std::time::Duration;

const INITIALIZATION_MAGIC: &[u8; 4] = b"MCG4";
const MAXIMUM_PATH_BYTES: usize = 16 * 1024;
pub(super) const READY: u8 = 1;
pub(super) const COMMIT: u8 = 2;
pub(super) const FINISH: u8 = 3;
const PRESERVE_TEMPORARY_DIRECTORY: u8 = 4;
const CLEANUP_COMPLETE: u8 = 5;
pub(super) const STOP: u8 = 6;
pub(super) const COMMITTED: u8 = 7;
const RETIREMENT_STARTED: u8 = 8;
const REMOVE_TEMPORARY_DIRECTORY: u8 = 9;

pub(super) enum OwnerDisposition {
    Finish,
    Stop,
    RetirementStarted,
    RemoveTemporaryDirectory,
    PreserveTemporaryDirectory,
    Closed,
    Failed(String),
}

pub(super) enum CleanupAcknowledgement {
    Complete,
    TimedOut,
}

pub(super) struct Initialization {
    pub(super) owner_pid: libc::pid_t,
    pub(super) root_pid: libc::pid_t,
    pub(super) cleanup_timeout: Duration,
    pub(super) temporary_directory: PathBuf,
}

pub(super) fn write(
    stream: &mut impl Write,
    owner_pid: libc::pid_t,
    root_pid: libc::pid_t,
    cleanup_timeout: Duration,
    temporary_directory: &Path,
) -> Result<(), String> {
    let cleanup_timeout_millis = cleanup_timeout_millis(cleanup_timeout)?;
    let path = temporary_directory.as_os_str().as_bytes();
    let path_length = u32::try_from(path.len())
        .ok()
        .filter(|length| *length as usize <= MAXIMUM_PATH_BYTES)
        .ok_or_else(|| "sandbox manager temporary path is too long".to_string())?;

    stream
        .write_all(INITIALIZATION_MAGIC)
        .and_then(|()| stream.write_all(&owner_pid.to_be_bytes()))
        .and_then(|()| stream.write_all(&root_pid.to_be_bytes()))
        .and_then(|()| stream.write_all(&cleanup_timeout_millis.to_be_bytes()))
        .and_then(|()| stream.write_all(&path_length.to_be_bytes()))
        .and_then(|()| stream.write_all(path))
        .map_err(|error| format!("failed to initialize sandbox manager: {error}"))
}

pub(super) fn read(stream: &mut impl Read) -> Result<Initialization, String> {
    let mut magic = [0; INITIALIZATION_MAGIC.len()];
    stream
        .read_exact(&mut magic)
        .map_err(|error| format!("failed to read sandbox manager initialization: {error}"))?;
    if &magic != INITIALIZATION_MAGIC {
        return Err("sandbox manager initialization had an invalid version".to_string());
    }

    let mut owner_pid = [0; std::mem::size_of::<libc::pid_t>()];
    let mut root_pid = [0; std::mem::size_of::<libc::pid_t>()];
    let mut cleanup_timeout = [0; std::mem::size_of::<u64>()];
    let mut path_length = [0; std::mem::size_of::<u32>()];
    stream
        .read_exact(&mut owner_pid)
        .and_then(|()| stream.read_exact(&mut root_pid))
        .and_then(|()| stream.read_exact(&mut cleanup_timeout))
        .and_then(|()| stream.read_exact(&mut path_length))
        .map_err(|error| format!("failed to read sandbox manager initialization: {error}"))?;

    let owner_pid = libc::pid_t::from_be_bytes(owner_pid);
    let root_pid = libc::pid_t::from_be_bytes(root_pid);
    let cleanup_timeout = u64::from_be_bytes(cleanup_timeout);
    let path_length = u32::from_be_bytes(path_length) as usize;
    if owner_pid <= 0 || root_pid <= 0 || cleanup_timeout == 0 || path_length > MAXIMUM_PATH_BYTES {
        return Err("sandbox manager initialization is invalid".to_string());
    }

    let mut path = vec![0; path_length];
    stream
        .read_exact(&mut path)
        .map_err(|error| format!("failed to read sandbox manager path: {error}"))?;
    Ok(Initialization {
        owner_pid,
        root_pid,
        cleanup_timeout: Duration::from_millis(cleanup_timeout),
        temporary_directory: PathBuf::from(OsString::from_vec(path)),
    })
}

pub(super) fn read_owner_disposition(stream: &mut impl Read) -> OwnerDisposition {
    let mut disposition = [0];
    loop {
        match stream.read(&mut disposition) {
            Ok(0) => return OwnerDisposition::Closed,
            Ok(_) if disposition == [FINISH] => return OwnerDisposition::Finish,
            Ok(_) if disposition == [STOP] => return OwnerDisposition::Stop,
            Ok(_) if disposition == [RETIREMENT_STARTED] => {
                return OwnerDisposition::RetirementStarted;
            }
            Ok(_) if disposition == [REMOVE_TEMPORARY_DIRECTORY] => {
                return OwnerDisposition::RemoveTemporaryDirectory;
            }
            Ok(_) if disposition == [PRESERVE_TEMPORARY_DIRECTORY] => {
                return OwnerDisposition::PreserveTemporaryDirectory;
            }
            Ok(_) => {
                return OwnerDisposition::Failed(
                    "sandbox manager received an invalid finish request".to_string(),
                );
            }
            Err(error) if error.kind() == ErrorKind::Interrupted => {}
            Err(error) => {
                return OwnerDisposition::Failed(format!(
                    "sandbox manager control failed: {error}"
                ));
            }
        }
    }
}

pub(super) fn write_retirement_started(stream: &mut impl Write) -> Result<(), String> {
    write_control(stream, RETIREMENT_STARTED)
}

pub(super) fn write_retirement_disposition(
    stream: &mut impl Write,
    preserve_temporary_directory: bool,
) -> Result<(), String> {
    write_control(
        stream,
        if preserve_temporary_directory {
            PRESERVE_TEMPORARY_DIRECTORY
        } else {
            REMOVE_TEMPORARY_DIRECTORY
        },
    )
}

pub(super) fn write_cleanup_complete(stream: &mut impl Write) -> Result<(), String> {
    stream
        .write_all(&[CLEANUP_COMPLETE])
        .map_err(|error| format!("failed to report sandbox manager cleanup: {error}"))
}

pub(super) fn read_cleanup_complete(
    stream: &mut impl Read,
) -> Result<CleanupAcknowledgement, String> {
    let mut acknowledgement = [0];
    match stream.read_exact(&mut acknowledgement) {
        Ok(()) if acknowledgement == [CLEANUP_COMPLETE] => Ok(CleanupAcknowledgement::Complete),
        Ok(()) => Err("sandbox manager sent an invalid cleanup response".to_string()),
        Err(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
            ) =>
        {
            Ok(CleanupAcknowledgement::TimedOut)
        }
        Err(error) => Err(format!(
            "sandbox manager exited before cleanup completed: {error}"
        )),
    }
}

fn write_control(stream: &mut impl Write, value: u8) -> Result<(), String> {
    stream
        .write_all(&[value])
        .map_err(|error| format!("failed to control sandbox manager retirement: {error}"))
}

pub(super) fn cleanup_timeout_millis(timeout: Duration) -> Result<u64, String> {
    timeout
        .as_millis()
        .checked_add(if timeout.subsec_nanos().is_multiple_of(1_000_000) {
            0
        } else {
            1
        })
        .and_then(|milliseconds| u64::try_from(milliseconds).ok())
        .filter(|milliseconds| *milliseconds > 0)
        .ok_or_else(|| "sandbox manager cleanup timeout is invalid".to_string())
}
