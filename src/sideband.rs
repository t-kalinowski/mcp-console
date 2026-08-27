use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, RawFd};
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use serde::Serialize;
use serde::de::DeserializeOwned;

const SIDEBAND_FD_ENV: &str = "MCP_CONSOLE_SIDEBAND_FD";
const READ_CHUNK_SIZE: usize = 8 * 1024;

static SIDEBAND_ALLOWED: AtomicBool = AtomicBool::new(true);
static FORK_FD: AtomicI32 = AtomicI32::new(-1);
static ATFORK_RESULT: OnceLock<libc::c_int> = OnceLock::new();

pub(crate) struct Reader {
    endpoint: Arc<UnixStream>,
    buffer: Vec<u8>,
    scanned: usize,
}

#[derive(Clone)]
pub(crate) struct Writer {
    endpoint: Arc<UnixStream>,
    serialization: Arc<Mutex<()>>,
}

pub(crate) struct ChildEndpoint {
    endpoint: UnixStream,
}

/// Creates the full-duplex Unix connection used for one worker sideband.
pub(crate) fn bind() -> io::Result<(Reader, Writer, ChildEndpoint)> {
    let (relay, worker) = UnixStream::pair()?;
    make_inheritable(worker.as_raw_fd())?;
    let (reader, writer) = split(relay);
    Ok((reader, writer, ChildEndpoint { endpoint: worker }))
}

/// Takes ownership of the sideband endpoint inherited by a worker.
pub(crate) fn connect_from_env() -> io::Result<(Reader, Writer)> {
    let descriptor = inherited_fd(SIDEBAND_FD_ENV)?;
    set_close_on_exec(descriptor, true)?;
    register_fork_cleanup(descriptor)?;

    // The descriptor numbers are bootstrap data, not part of the R session.
    unsafe {
        std::env::remove_var(SIDEBAND_FD_ENV);
    }

    // SAFETY: the bootstrap descriptor is the live worker endpoint, and this
    // process takes sole ownership of it exactly once.
    let endpoint = unsafe { UnixStream::from_raw_fd(descriptor) };
    Ok(split(endpoint))
}

fn split(endpoint: UnixStream) -> (Reader, Writer) {
    let endpoint = Arc::new(endpoint);
    (Reader::new(endpoint.clone()), Writer::new(endpoint))
}

impl Reader {
    fn new(endpoint: Arc<UnixStream>) -> Self {
        Self {
            endpoint,
            buffer: Vec::new(),
            scanned: 0,
        }
    }

    pub(crate) fn has_buffered_data(&self) -> bool {
        !self.buffer.is_empty()
    }

    /// Receives one newline-delimited JSON message from the worker.
    pub(crate) fn receive<T: DeserializeOwned>(&mut self) -> io::Result<T> {
        loop {
            if let Some(message) = self.take_message()? {
                return Ok(message);
            }
            match self.read_chunk() {
                Ok(()) => {}
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error),
            }
        }
    }

    /// Returns one complete frame already assembled from prior reads.
    pub(crate) fn receive_buffered<T: DeserializeOwned>(&mut self) -> io::Result<Option<T>> {
        self.take_message()
    }

    /// Reads one chunk after the caller observes descriptor readiness.
    pub(crate) fn read_chunk(&mut self) -> io::Result<()> {
        let mut buffer = [0; READ_CHUNK_SIZE];
        let mut endpoint = self.endpoint.as_ref();
        let length = match endpoint.read(&mut buffer) {
            Ok(length) => length,
            Err(error) if error.kind() == io::ErrorKind::ConnectionReset => 0,
            Err(error) => return Err(error),
        };
        self.append_chunk(&buffer[..length])
    }

    /// Reads one retirement chunk without changing the endpoint's blocking mode.
    pub(crate) fn read_chunk_nonblocking(&mut self) -> io::Result<()> {
        let mut buffer = [0; READ_CHUNK_SIZE];
        // SAFETY: the endpoint remains live through `self`, and the buffer names
        // writable initialized storage for exactly the supplied length.
        let length = unsafe {
            libc::recv(
                self.endpoint.as_raw_fd(),
                buffer.as_mut_ptr().cast(),
                buffer.len(),
                libc::MSG_DONTWAIT,
            )
        };
        if length < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::ConnectionReset {
                return self.append_chunk(&[]);
            }
            return Err(error);
        }
        self.append_chunk(&buffer[..length as usize])
    }

    fn append_chunk(&mut self, chunk: &[u8]) -> io::Result<()> {
        match chunk {
            [] if self.buffer.is_empty() => Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "worker sideband closed",
            )),
            [] => Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "worker sideband closed midway through a frame",
            )),
            chunk => {
                self.buffer.extend_from_slice(chunk);
                Ok(())
            }
        }
    }

    fn take_message<T: DeserializeOwned>(&mut self) -> io::Result<Option<T>> {
        let Some(newline) = self.buffer[self.scanned..]
            .iter()
            .position(|byte| *byte == b'\n')
            .map(|newline| self.scanned + newline)
        else {
            self.scanned = self.buffer.len();
            return Ok(None);
        };
        let mut line = self.buffer.drain(..=newline).collect::<Vec<_>>();
        self.scanned = 0;
        self.buffer.shrink_to(READ_CHUNK_SIZE);
        line.pop();
        if line.last() == Some(&b'\r') {
            line.pop();
        }
        serde_json::from_slice(&line)
            .map(Some)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
    }
}

impl AsRawFd for Reader {
    fn as_raw_fd(&self) -> RawFd {
        self.endpoint.as_raw_fd()
    }
}

impl Writer {
    fn new(endpoint: Arc<UnixStream>) -> Self {
        Self {
            endpoint,
            serialization: Arc::new(Mutex::new(())),
        }
    }

    /// Sends and flushes one newline-delimited JSON message to the worker.
    pub(crate) fn send<T: Serialize>(&self, message: &T) -> io::Result<()> {
        let _serialization = self
            .serialization
            .lock()
            .map_err(|_| io::Error::other("worker sideband writer lock poisoned"))?;
        let mut endpoint = self.endpoint.as_ref();
        serde_json::to_writer(&mut endpoint, message)?;
        endpoint.write_all(b"\n")?;
        endpoint.flush()
    }
}

impl ChildEndpoint {
    /// Passes the inheritable worker endpoint to an ordinary child process.
    pub(crate) fn configure_process(&self, command: &mut std::process::Command) {
        command.env(SIDEBAND_FD_ENV, self.endpoint.as_raw_fd().to_string());
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
    let descriptor = FORK_FD.swap(-1, Ordering::SeqCst);
    // Closing only this process's descriptor leaves the parent's shared socket
    // endpoint intact. `shutdown()` here would change the parent's endpoint too.
    unsafe {
        if descriptor >= 0 {
            libc::close(descriptor);
        }
    }
}

fn register_fork_cleanup(descriptor: RawFd) -> io::Result<()> {
    // CLOEXEC does not close this descriptor in fork-only R descendants.
    let result = *ATFORK_RESULT.get_or_init(|| unsafe {
        libc::pthread_atfork(None, None, Some(close_sideband_in_fork_child))
    });
    if result != 0 {
        return Err(io::Error::from_raw_os_error(result));
    }
    FORK_FD.store(descriptor, Ordering::SeqCst);
    Ok(())
}
