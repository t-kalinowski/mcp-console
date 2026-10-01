//! Preserve suspended sideband order without retaining the stream in memory.

use std::fs::File;
use std::io::{BufRead as _, BufReader, Seek as _, SeekFrom, Write as _};
use std::os::fd::FromRawFd as _;
use std::os::unix::ffi::OsStrExt as _;

use crate::relay_protocol::RelayEvent;

#[derive(Default)]
pub(super) struct DeferredEvents {
    spool: Option<Spool>,
}

struct Spool {
    file: File,
    read: u64,
    written: u64,
}

impl DeferredEvents {
    pub(super) fn is_empty(&self) -> bool {
        self.spool.is_none()
    }

    pub(super) fn push(&mut self, event: &RelayEvent) -> Result<(), String> {
        if self.spool.is_none() {
            self.spool = Some(Spool::create().map_err(spool_error)?);
        }
        let spool = self.spool.as_mut().expect("deferred event spool");
        spool
            .file
            .seek(SeekFrom::Start(spool.written))
            .map_err(spool_error)?;
        serde_json::to_writer(&mut spool.file, event).map_err(spool_error)?;
        spool.file.write_all(b"\n").map_err(spool_error)?;
        spool.written = spool.file.stream_position().map_err(spool_error)?;
        Ok(())
    }

    pub(super) fn pop(&mut self) -> Result<Option<RelayEvent>, String> {
        let Some(spool) = self.spool.as_mut() else {
            return Ok(None);
        };
        spool
            .file
            .seek(SeekFrom::Start(spool.read))
            .map_err(spool_error)?;
        let mut line = String::new();
        let bytes = BufReader::new(&mut spool.file)
            .read_line(&mut line)
            .map_err(spool_error)?;
        if bytes == 0 {
            return Err("deferred bootstrap spool ended before its final event".into());
        }
        let event = serde_json::from_str(&line).map_err(spool_error)?;
        spool.read += bytes as u64;
        if spool.read == spool.written {
            self.clear();
        }
        Ok(Some(event))
    }

    pub(super) fn clear(&mut self) {
        self.spool = None;
    }
}

impl Spool {
    fn create() -> std::io::Result<Self> {
        let path = std::env::temp_dir().join("mcp-console-bootstrap-XXXXXX");
        let mut template = path.as_os_str().as_bytes().to_vec();
        template.push(0);
        // SAFETY: mkstemp creates a unique mode-0600 file and transfers its fd.
        let descriptor = unsafe { libc::mkstemp(template.as_mut_ptr().cast()) };
        if descriptor < 0 {
            return Err(std::io::Error::last_os_error());
        }
        // SAFETY: the successfully created descriptor is uniquely owned here.
        let file = unsafe { File::from_raw_fd(descriptor) };
        // Unlink immediately: retirement, failure, or controller exit releases
        // the spool without leaving startup output in a named temporary file.
        // SAFETY: template contains the terminated path returned by mkstemp.
        if unsafe { libc::unlink(template.as_ptr().cast()) } < 0 {
            return Err(std::io::Error::last_os_error());
        }
        // SAFETY: descriptor belongs to file; no child may inherit it.
        if unsafe { libc::fcntl(descriptor, libc::F_SETFD, libc::FD_CLOEXEC) } < 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(Self {
            file,
            read: 0,
            written: 0,
        })
    }
}

fn spool_error(error: impl std::fmt::Display) -> String {
    format!("deferred bootstrap spool failed: {error}")
}
