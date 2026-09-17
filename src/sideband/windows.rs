use crate::windows::{Event, Pipe};
use serde::{Serialize, de::DeserializeOwned};
use std::io::{self, BufRead, BufReader, Write};
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::sync::{Arc, Mutex};

const READ_HANDLE: &str = "MCP_CONSOLE_SIDEBAND_READ_HANDLE";
const WRITE_HANDLE: &str = "MCP_CONSOLE_SIDEBAND_WRITE_HANDLE";

pub(crate) struct Reader(BufReader<Pipe>);
#[derive(Clone)]
pub(crate) struct Writer(Arc<Mutex<Pipe>>);
pub(crate) struct ChildEndpoints(OwnedHandle, OwnedHandle);

pub(crate) fn bind(cancel: Event) -> io::Result<(Reader, Writer, ChildEndpoints)> {
    let (worker_reader, relay_writer) = crate::windows::pipe(true, true)?;
    let (relay_reader, worker_writer) = crate::windows::pipe(true, true)?;
    crate::windows::inherit(worker_reader.as_raw_handle(), true)?;
    crate::windows::inherit(worker_writer.as_raw_handle(), true)?;
    let reader = Pipe::from(relay_reader).with_cancel(cancel.clone());
    let writer = Pipe::from(relay_writer).with_cancel(cancel);
    Ok((
        Reader(BufReader::new(reader)),
        Writer(Arc::new(Mutex::new(writer))),
        ChildEndpoints(worker_reader, worker_writer),
    ))
}

pub(crate) fn connect_from_env() -> io::Result<(Reader, Writer)> {
    fn adopt(name: &str) -> io::Result<OwnedHandle> {
        let value = std::env::var(name).map_err(io::Error::other)?;
        let handle = value.parse::<usize>().map_err(io::Error::other)? as *mut std::ffi::c_void;
        if handle.is_null() || handle as isize == -1 {
            return Err(io::Error::other("invalid sideband handle"));
        }
        crate::windows::inherit(handle, false)?;
        unsafe {
            std::env::remove_var(name);
        }
        Ok(unsafe { OwnedHandle::from_raw_handle(handle) })
    }
    let read = std::env::var(READ_HANDLE).map_err(io::Error::other)?;
    if std::env::var(WRITE_HANDLE).as_deref() == Ok(&read) {
        return Err(io::Error::other("sideband handles must be distinct"));
    }
    Ok((
        Reader(BufReader::new(Pipe::from(adopt(READ_HANDLE)?))),
        Writer(Arc::new(Mutex::new(Pipe::from(adopt(WRITE_HANDLE)?)))),
    ))
}

impl Reader {
    pub(crate) fn receive<T: DeserializeOwned>(&mut self) -> io::Result<T> {
        let mut bytes = Vec::new();
        if self.0.read_until(b'\n', &mut bytes)? == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "worker sideband closed",
            ));
        }
        if bytes.last() != Some(&b'\n') {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "worker sideband closed midway through a frame",
            ));
        }
        serde_json::from_slice(&bytes).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
    }
}
impl Writer {
    pub(crate) fn send<T: Serialize>(&self, message: &T) -> io::Result<()> {
        let mut bytes = serde_json::to_vec(message)?;
        bytes.push(b'\n');
        self.0
            .lock()
            .map_err(|_| io::Error::other("sideband writer lock poisoned"))?
            .write_all(&bytes)
    }
}
impl ChildEndpoints {
    pub(crate) fn configure_process(&self, command: &mut std::process::Command) {
        command
            .env(READ_HANDLE, (self.0.as_raw_handle() as usize).to_string())
            .env(WRITE_HANDLE, (self.1.as_raw_handle() as usize).to_string());
    }
}

pub(crate) fn available_in_process() -> bool {
    true
}
