use std::ffi::OsString;
use std::io::{Read, Write};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};
use std::time::Duration;

const INITIALIZATION_MAGIC: &[u8; 4] = b"MCG3";
const MAXIMUM_PATH_BYTES: usize = 16 * 1024;

pub(super) struct Initialization {
    pub(super) owner_pid: libc::pid_t,
    pub(super) root_pid: libc::pid_t,
    pub(super) cleanup_timeout: Duration,
    pub(super) separate_process_group: bool,
    pub(super) temporary_directory: PathBuf,
}

pub(super) fn write(
    stream: &mut impl Write,
    owner_pid: libc::pid_t,
    root_pid: libc::pid_t,
    cleanup_timeout: Duration,
    separate_process_group: bool,
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
        .and_then(|()| stream.write_all(&[u8::from(separate_process_group)]))
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
    let mut separate_process_group = [0];
    let mut path_length = [0; std::mem::size_of::<u32>()];
    stream
        .read_exact(&mut owner_pid)
        .and_then(|()| stream.read_exact(&mut root_pid))
        .and_then(|()| stream.read_exact(&mut cleanup_timeout))
        .and_then(|()| stream.read_exact(&mut separate_process_group))
        .and_then(|()| stream.read_exact(&mut path_length))
        .map_err(|error| format!("failed to read sandbox manager initialization: {error}"))?;

    let owner_pid = libc::pid_t::from_be_bytes(owner_pid);
    let root_pid = libc::pid_t::from_be_bytes(root_pid);
    let cleanup_timeout = u64::from_be_bytes(cleanup_timeout);
    let separate_process_group = match separate_process_group[0] {
        0 => false,
        1 => true,
        _ => return Err("sandbox manager initialization is invalid".to_string()),
    };
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
        separate_process_group,
        temporary_directory: PathBuf::from(OsString::from_vec(path)),
    })
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
