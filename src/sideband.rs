use std::ffi::c_int;
use std::fs::File;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, IntoRawFd, OwnedFd, RawFd};
use std::process::Command;
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use serde::Serialize;
use serde::de::DeserializeOwned;

const READ_FD_ENV: &str = "MCP_CONSOLE_SIDEBAND_READ_FD";
const WRITE_FD_ENV: &str = "MCP_CONSOLE_SIDEBAND_WRITE_FD";

static SIDEBAND_ALLOWED: AtomicBool = AtomicBool::new(true);
static FORK_READ_FD: AtomicI32 = AtomicI32::new(-1);
static FORK_WRITE_FD: AtomicI32 = AtomicI32::new(-1);
static ATFORK_RESULT: OnceLock<c_int> = OnceLock::new();

pub struct Reader {
    inner: BufReader<Box<dyn Read + Send>>,
}

#[derive(Clone)]
pub struct Writer {
    inner: Arc<Mutex<Box<dyn Write + Send>>>,
}

pub struct ChildFds {
    read: OwnedFd,
    write: OwnedFd,
}

pub fn bind() -> io::Result<(Reader, Writer, ChildFds)> {
    let (server_read, child_write) = std::io::pipe()?;
    let (child_read, server_write) = std::io::pipe()?;

    let child_read = unsafe { OwnedFd::from_raw_fd(child_read.into_raw_fd()) };
    let child_write = unsafe { OwnedFd::from_raw_fd(child_write.into_raw_fd()) };
    set_close_on_exec(child_read.as_raw_fd(), false)?;
    set_close_on_exec(child_write.as_raw_fd(), false)?;

    Ok((
        Reader::new(server_read),
        Writer::new(server_write),
        ChildFds {
            read: child_read,
            write: child_write,
        },
    ))
}

pub fn connect_from_env() -> io::Result<(Reader, Writer)> {
    let read = inherited_fd(READ_FD_ENV)?;
    let write = inherited_fd(WRITE_FD_ENV)?;
    set_close_on_exec(read, true)?;
    set_close_on_exec(write, true)?;
    register_fork_cleanup(read, write)?;

    // The descriptor numbers are bootstrap data, not part of the R session.
    unsafe {
        std::env::remove_var(READ_FD_ENV);
        std::env::remove_var(WRITE_FD_ENV);
    }

    let read = unsafe { File::from_raw_fd(read) };
    let write = unsafe { File::from_raw_fd(write) };
    Ok((Reader::new(read), Writer::new(write)))
}

pub fn set_inherited_close_on_exec(enabled: bool) -> io::Result<()> {
    set_close_on_exec(inherited_fd(READ_FD_ENV)?, enabled)?;
    set_close_on_exec(inherited_fd(WRITE_FD_ENV)?, enabled)
}

impl Reader {
    fn new(reader: impl Read + Send + 'static) -> Self {
        Self {
            inner: BufReader::new(Box::new(reader)),
        }
    }

    pub fn receive<T: DeserializeOwned>(&mut self) -> io::Result<T> {
        let mut line = String::new();
        if self.inner.read_line(&mut line)? == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "worker sideband closed",
            ));
        }

        serde_json::from_str(line.trim_end_matches(['\n', '\r']))
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
    }
}

impl Writer {
    fn new(writer: impl Write + Send + 'static) -> Self {
        Self {
            inner: Arc::new(Mutex::new(Box::new(writer))),
        }
    }

    pub fn send<T: Serialize>(&self, message: &T) -> io::Result<()> {
        if !available_in_process() {
            return Err(io::Error::new(
                io::ErrorKind::BrokenPipe,
                "worker sideband is unavailable in a forked child",
            ));
        }
        let mut writer = self
            .inner
            .lock()
            .map_err(|_| io::Error::other("worker sideband writer lock poisoned"))?;
        serde_json::to_writer(&mut **writer, message)?;
        writer.write_all(b"\n")?;
        writer.flush()
    }
}

pub fn available_in_process() -> bool {
    SIDEBAND_ALLOWED.load(Ordering::SeqCst)
}

impl ChildFds {
    pub fn configure(&self, command: &mut Command) {
        command.env(READ_FD_ENV, self.read.as_raw_fd().to_string());
        command.env(WRITE_FD_ENV, self.write.as_raw_fd().to_string());
    }
}

fn inherited_fd(name: &str) -> io::Result<RawFd> {
    std::env::var(name)
        .map_err(|_| io::Error::new(io::ErrorKind::NotFound, format!("{name} is missing")))?
        .parse()
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, format!("{name} is invalid")))
}

fn set_close_on_exec(fd: RawFd, enabled: bool) -> io::Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 {
        return Err(io::Error::last_os_error());
    }
    let flags = if enabled {
        flags | libc::FD_CLOEXEC
    } else {
        flags & !libc::FD_CLOEXEC
    };
    if unsafe { libc::fcntl(fd, libc::F_SETFD, flags) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

extern "C" fn close_sideband_in_fork_child() {
    SIDEBAND_ALLOWED.store(false, Ordering::SeqCst);
    let read = FORK_READ_FD.swap(-1, Ordering::SeqCst);
    let write = FORK_WRITE_FD.swap(-1, Ordering::SeqCst);
    unsafe {
        if read >= 0 {
            libc::close(read);
        }
        if write >= 0 {
            libc::close(write);
        }
    }
}

fn register_fork_cleanup(read: RawFd, write: RawFd) -> io::Result<()> {
    // CLOEXEC does not close these descriptors in fork-only R descendants.
    let result = *ATFORK_RESULT.get_or_init(|| unsafe {
        libc::pthread_atfork(None, None, Some(close_sideband_in_fork_child))
    });
    if result != 0 {
        return Err(io::Error::from_raw_os_error(result));
    }
    FORK_READ_FD.store(read, Ordering::SeqCst);
    FORK_WRITE_FD.store(write, Ordering::SeqCst);
    Ok(())
}
