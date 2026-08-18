use std::fs::File;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use serde::Serialize;
use serde::de::DeserializeOwned;

const READ_FD_ENV: &str = "MCP_CONSOLE_SIDEBAND_READ_FD";
const WRITE_FD_ENV: &str = "MCP_CONSOLE_SIDEBAND_WRITE_FD";

static SIDEBAND_ALLOWED: AtomicBool = AtomicBool::new(true);
static FORK_READ_FD: AtomicI32 = AtomicI32::new(-1);
static FORK_WRITE_FD: AtomicI32 = AtomicI32::new(-1);
static ATFORK_RESULT: OnceLock<libc::c_int> = OnceLock::new();

pub(crate) struct Reader {
    inner: BufReader<Box<dyn Read + Send>>,
    raw_fd: RawFd,
}

#[derive(Clone)]
pub(crate) struct Writer {
    inner: Arc<Mutex<Box<dyn Write + Send>>>,
}

pub(crate) struct ChildFds {
    read: OwnedFd,
    write: OwnedFd,
}

/// Creates the two inherited pipes used for one duplex worker sideband.
pub(crate) fn bind() -> io::Result<(Reader, Writer, ChildFds)> {
    let (server_read, child_write) = std::io::pipe()?;
    let (child_read, server_write) = std::io::pipe()?;

    let child_read: OwnedFd = child_read.into();
    let child_write: OwnedFd = child_write.into();
    make_inheritable(child_read.as_raw_fd())?;
    make_inheritable(child_write.as_raw_fd())?;

    Ok((
        Reader::new(server_read),
        Writer::new(server_write),
        ChildFds {
            read: child_read,
            write: child_write,
        },
    ))
}

/// Takes ownership of the sideband endpoints inherited by a worker.
pub(crate) fn connect_from_env() -> io::Result<(Reader, Writer)> {
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

impl Reader {
    fn new(reader: impl Read + AsRawFd + Send + 'static) -> Self {
        let raw_fd = reader.as_raw_fd();
        Self {
            inner: BufReader::new(Box::new(reader)),
            raw_fd,
        }
    }

    pub(crate) fn has_buffered_data(&self) -> bool {
        !self.inner.buffer().is_empty()
    }

    /// Receives one newline-delimited JSON message from the worker.
    pub(crate) fn receive<T: DeserializeOwned>(&mut self) -> io::Result<T> {
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

impl AsRawFd for Reader {
    fn as_raw_fd(&self) -> RawFd {
        self.raw_fd
    }
}

impl Writer {
    fn new(writer: impl Write + Send + 'static) -> Self {
        Self {
            inner: Arc::new(Mutex::new(Box::new(writer))),
        }
    }

    /// Sends and flushes one newline-delimited JSON message to the worker.
    pub(crate) fn send<T: Serialize>(&self, message: &T) -> io::Result<()> {
        let mut writer = self
            .inner
            .lock()
            .map_err(|_| io::Error::other("worker sideband writer lock poisoned"))?;
        serde_json::to_writer(&mut **writer, message)?;
        writer.write_all(b"\n")?;
        writer.flush()
    }
}

impl ChildFds {
    /// Passes the inheritable worker endpoints to a child through its environment.
    pub(crate) fn configure(&self, command: &mut crate::sandbox::SandboxedCommand) {
        command.env(READ_FD_ENV, self.read.as_raw_fd().to_string());
        command.env(WRITE_FD_ENV, self.write.as_raw_fd().to_string());
    }
}

fn make_inheritable(fd: RawFd) -> io::Result<()> {
    set_close_on_exec(fd, false)
}

fn inherited_fd(name: &str) -> io::Result<RawFd> {
    std::env::var(name)
        .map_err(|_| io::Error::new(io::ErrorKind::NotFound, format!("{name} is missing")))?
        .parse()
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, format!("{name} is invalid")))
}

fn set_close_on_exec(fd: RawFd, enabled: bool) -> io::Result<()> {
    // SAFETY: the caller owns the live descriptor, and F_GETFD does not modify memory.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 {
        return Err(io::Error::last_os_error());
    }
    let flags = if enabled {
        flags | libc::FD_CLOEXEC
    } else {
        flags & !libc::FD_CLOEXEC
    };
    // SAFETY: `fd` remains live, and F_SETFD receives the flags read above.
    if unsafe { libc::fcntl(fd, libc::F_SETFD, flags) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

pub(crate) fn available_in_process() -> bool {
    SIDEBAND_ALLOWED.load(Ordering::SeqCst)
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
