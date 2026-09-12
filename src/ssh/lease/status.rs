//! Local-only adapter records. They never enter the remote application stream,
//! transcripts, or tool output, and may split an incomplete inner protocol frame.

use std::collections::VecDeque;
use std::fs::File;
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::sync::{Arc, Mutex};

use super::{BLOCK, frame, invalid};

#[derive(Clone, Default)]
pub(crate) struct Status(Arc<Mutex<[bool; 2]>>);

impl Status {
    pub fn set(&self, preparation: bool, recovering: bool) {
        self.0.lock().expect("SSH connection status lock")[usize::from(preparation)] = recovering;
    }
    pub fn check(&self) -> Result<(), String> {
        let states = self.0.lock().map_err(|_| "SSH connection status lock")?;
        if states.iter().any(|state| *state) {
            Err("SSH connection recovery is in progress; new work was not admitted; poll the current operation".into())
        } else {
            Ok(())
        }
    }
}

pub(crate) struct Output<R> {
    reader: R,
    session: crate::ssh::Session,
    preparation: bool,
    pending: io::Cursor<Vec<u8>>,
}

impl<R: Read> Output<R> {
    pub fn new(reader: R, session: crate::ssh::Session, preparation: bool) -> Self {
        Self {
            reader,
            session,
            preparation,
            pending: io::Cursor::new(Vec::new()),
        }
    }
}

impl<R: Read> Read for Output<R> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        if buffer.is_empty() {
            return Ok(0);
        }
        loop {
            let count = self.pending.read(buffer)?;
            if count > 0 {
                return Ok(count);
            }
            let mut tag = [0];
            if self.reader.read(&mut tag)? == 0 {
                self.session.status.set(self.preparation, false);
                return Ok(0);
            }
            let bytes = crate::ssh::read_payload(&mut self.reader, BLOCK)?;
            match (tag[0], bytes.as_slice()) {
                (1, _) if !bytes.is_empty() => self.pending = io::Cursor::new(bytes),
                (2, b"connected") => self.session.status.set(self.preparation, false),
                (2, b"recovering") => self.session.status.set(self.preparation, true),
                (2, b"unconfirmed") => {
                    self.session.block();
                    self.session.status.set(self.preparation, false);
                }
                (2, b"retiring") => {}
                _ => return Err(invalid("invalid local SSH adapter record")),
            }
        }
    }
}

pub(super) struct Writer {
    file: File,
    encoded: VecDeque<u8>,
    application: usize,
    status: Option<&'static str>,
}

impl Writer {
    pub fn new(file: File) -> Self {
        Self {
            file,
            encoded: VecDeque::new(),
            application: 0,
            status: None,
        }
    }
    pub fn status(&mut self, status: &'static str) {
        self.status = Some(status);
    }
    pub fn pending(&self) -> bool {
        !self.encoded.is_empty() || self.status.is_some()
    }
    pub fn service(&mut self) -> io::Result<()> {
        if self.application == 0 {
            if self.encoded.is_empty()
                && let Some(status) = self.status.take()
            {
                self.encoded.extend(frame(2, status.as_bytes()));
            }
            super::stream::write(&mut self.file, &mut self.encoded)?;
        }
        Ok(())
    }
}

impl AsRawFd for Writer {
    fn as_raw_fd(&self) -> i32 {
        self.file.as_raw_fd()
    }
}
impl Write for Writer {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if self.application == 0 {
            self.service()?;
            if !self.encoded.is_empty() || self.status.is_some() {
                return Err(io::ErrorKind::WouldBlock.into());
            }
            assert!(bytes.len() <= BLOCK);
            self.encoded.extend(frame(1, bytes));
            self.application = bytes.len();
        }
        super::stream::write(&mut self.file, &mut self.encoded)?;
        if !self.encoded.is_empty() {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        Ok(std::mem::take(&mut self.application))
    }
    fn flush(&mut self) -> io::Result<()> {
        self.file.flush()
    }
}
