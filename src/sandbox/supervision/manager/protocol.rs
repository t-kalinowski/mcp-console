use std::ffi::OsString;
use std::io::{Read, Write};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};
use std::time::Duration;

const INITIALIZATION_MAGIC: &[u8; 4] = b"MCG4";
const MAXIMUM_PATH_BYTES: usize = 16 * 1024;
const RETIREMENT_STARTED: u8 = 2;
const REMOVE_TEMPORARY_DIRECTORY: u8 = 3;
const PRESERVE_TEMPORARY_DIRECTORY: u8 = 4;
const CLEANUP_COMPLETE: u8 = 5;

pub(super) enum RetirementCommand {
    Started,
    RemoveTemporaryDirectory,
    PreserveTemporaryDirectory,
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
    write_control(stream, CLEANUP_COMPLETE)
}

pub(super) fn read_cleanup_complete(stream: &mut impl Read) -> Result<(), String> {
    match read_control(stream)? {
        Some(CLEANUP_COMPLETE) => Ok(()),
        Some(_) => Err("sandbox manager sent an invalid cleanup response".to_string()),
        None => Err("sandbox manager exited before cleanup completed".to_string()),
    }
}

pub(super) fn read_retirement_command(
    stream: &mut impl Read,
) -> Result<Option<RetirementCommand>, String> {
    match read_control(stream)? {
        Some(RETIREMENT_STARTED) => Ok(Some(RetirementCommand::Started)),
        Some(REMOVE_TEMPORARY_DIRECTORY) => Ok(Some(RetirementCommand::RemoveTemporaryDirectory)),
        Some(PRESERVE_TEMPORARY_DIRECTORY) => {
            Ok(Some(RetirementCommand::PreserveTemporaryDirectory))
        }
        Some(_) => Err("sandbox manager received an invalid retirement command".to_string()),
        None => Ok(None),
    }
}

fn write_control(stream: &mut impl Write, value: u8) -> Result<(), String> {
    stream
        .write_all(&[value])
        .map_err(|error| format!("failed to control sandbox manager retirement: {error}"))
}

fn read_control(stream: &mut impl Read) -> Result<Option<u8>, String> {
    let mut value = [0];
    loop {
        match stream.read(&mut value) {
            Ok(0) => return Ok(None),
            Ok(1) => return Ok(Some(value[0])),
            Ok(_) => unreachable!("one-byte sandbox manager read returned excess data"),
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {}
            Err(error) => {
                return Err(format!(
                    "failed to control sandbox manager retirement: {error}"
                ));
            }
        }
    }
}

fn cleanup_timeout_millis(timeout: Duration) -> Result<u64, String> {
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
