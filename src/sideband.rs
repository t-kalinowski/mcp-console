use std::io::{self, BufRead, BufReader, PipeReader, PipeWriter, Write};
use std::os::fd::{AsRawFd, OwnedFd};
use std::sync::{Arc, Mutex};

use serde::Serialize;
use serde::de::DeserializeOwned;

const READ_FD_ENV: &str = "MCP_CONSOLE_SIDEBAND_READ_FD";
const WRITE_FD_ENV: &str = "MCP_CONSOLE_SIDEBAND_WRITE_FD";

pub(crate) struct Reader {
    inner: BufReader<PipeReader>,
}

#[derive(Clone)]
pub(crate) struct Writer {
    inner: Arc<Mutex<PipeWriter>>,
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

    Ok((
        Reader {
            inner: BufReader::new(server_read),
        },
        Writer {
            inner: Arc::new(Mutex::new(server_write)),
        },
        ChildFds {
            read: child_read,
            write: child_write,
        },
    ))
}

impl Reader {
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

impl Writer {
    /// Sends and flushes one newline-delimited JSON message to the worker.
    pub(crate) fn send<T: Serialize>(&self, message: &T) -> io::Result<()> {
        let mut writer = self
            .inner
            .lock()
            .map_err(|_| io::Error::other("worker sideband writer lock poisoned"))?;
        serde_json::to_writer(&mut *writer, message)?;
        writer.write_all(b"\n")?;
        writer.flush()
    }
}

impl ChildFds {
    /// Passes the inheritable worker endpoints to a child through its environment.
    pub(crate) fn configure(self, command: &mut crate::sandbox::SandboxedCommand) {
        command.env(READ_FD_ENV, self.read.as_raw_fd().to_string());
        command.env(WRITE_FD_ENV, self.write.as_raw_fd().to_string());
        command.inherit_descriptors([self.read, self.write]);
    }
}
