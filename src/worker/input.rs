use std::collections::VecDeque;
use std::ffi::{c_int, c_uchar};
use std::io;
use std::os::fd::{AsRawFd, IntoRawFd, RawFd};
use std::sync::{Mutex, OnceLock};

use super::core;

static INTERRUPT_WAKEUP: OnceLock<io::PipeReader> = OnceLock::new();

pub(super) fn initialize_interrupt_wakeup() -> io::Result<c_int> {
    let (reader, writer) = io::pipe()?;
    for descriptor in [reader.as_raw_fd(), writer.as_raw_fd()] {
        if unsafe { libc::fcntl(descriptor, libc::F_SETFL, libc::O_NONBLOCK) } < 0 {
            return Err(io::Error::last_os_error());
        }
    }
    INTERRUPT_WAKEUP
        .set(reader)
        .map_err(|_| io::Error::other("interrupt wakeup already initialized"))?;
    // The signal handler retains this descriptor for the worker lifetime.
    Ok(writer.into_raw_fd())
}

pub(super) fn interrupt_wakeup_fd() -> RawFd {
    INTERRUPT_WAKEUP
        .get()
        .expect("interrupt wakeup initialized")
        .as_raw_fd()
}

static CONSOLE_STDIN: Mutex<ConsoleStdin> = Mutex::new(ConsoleStdin {
    pushback: VecDeque::new(),
    line_prefix: Vec::new(),
});
struct ConsoleStdin {
    pushback: VecDeque<ConsoleStdinChunk>,
    line_prefix: Vec<u8>,
}

struct ConsoleStdinChunk {
    bytes: Vec<u8>,
    offset: usize,
}

impl ConsoleStdin {
    unsafe fn copy_pushback(&mut self, destination: *mut u8, capacity: usize) -> usize {
        let mut copied = 0;
        while copied < capacity {
            let Some(chunk) = self.pushback.front_mut() else {
                break;
            };
            debug_assert!(chunk.offset <= chunk.bytes.len());
            let remaining = &chunk.bytes[chunk.offset..];
            let line_length = remaining
                .iter()
                .position(|byte| *byte == b'\n')
                .map_or(remaining.len(), |index| index + 1);
            let length = line_length.min(capacity - copied);
            let complete = remaining[length - 1] == b'\n';
            unsafe {
                std::ptr::copy_nonoverlapping(remaining.as_ptr(), destination.add(copied), length);
            }
            copied += length;
            chunk.offset += length;
            if chunk.offset == chunk.bytes.len() {
                self.pushback.pop_front();
            }
            if complete {
                break;
            }
        }
        if self.pushback.is_empty() {
            self.pushback = VecDeque::new();
        }
        copied
    }

    fn record_chunk(&mut self, chunk: &[u8]) {
        if chunk.last() == Some(&b'\n') {
            self.line_prefix = Vec::new();
        } else {
            self.line_prefix.extend_from_slice(chunk);
        }
    }

    fn preserve_line(&mut self, chunk: &[u8]) {
        if self.line_prefix.is_empty() && chunk.is_empty() {
            return;
        }
        if !chunk.is_empty() {
            self.pushback.push_front(ConsoleStdinChunk {
                bytes: chunk.to_vec(),
                offset: 0,
            });
        }
        if !self.line_prefix.is_empty() {
            self.pushback.push_front(ConsoleStdinChunk {
                bytes: std::mem::take(&mut self.line_prefix),
                offset: 0,
            });
        }
    }

    fn finish_operation(&mut self) {
        // A later callback cannot be assumed to continue this operation.
        self.preserve_line(&[]);
    }
}

fn console_eof(buf: *mut c_uchar) -> c_int {
    unsafe {
        *buf = 0;
    }
    0
}

pub(super) fn read_console_stdin(
    buf: *mut c_uchar,
    buflen: c_int,
    interrupted: impl Fn() -> bool,
) -> Result<c_int, String> {
    let capacity = (buflen as usize) - 1;
    if interrupted() {
        return cancel_console_stdin_read(buf, 0);
    }
    let mut stdin = CONSOLE_STDIN
        .lock()
        .map_err(|_| "R worker console stdin lock poisoned".to_string())?;
    // SAFETY: callers provide buflen writable bytes, reserving one for NUL.
    let mut length = unsafe { stdin.copy_pushback(buf, capacity) };
    drop(stdin);

    while length < capacity && (length == 0 || unsafe { *buf.add(length - 1) } != b'\n') {
        if interrupted() {
            return cancel_console_stdin_read(buf, length);
        }
        let wakeup = INTERRUPT_WAKEUP
            .get()
            .expect("interrupt wakeup initialized");
        let mut descriptors = [
            libc::pollfd {
                fd: libc::STDIN_FILENO,
                events: libc::POLLIN,
                revents: 0,
            },
            libc::pollfd {
                fd: wakeup.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            },
        ];
        let ready = unsafe { libc::poll(descriptors.as_mut_ptr(), 2, -1) };
        if ready == 0 {
            continue;
        }
        if ready < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(format!("R worker stdin poll failed: {error}"));
        }
        if descriptors[1].revents != 0 {
            let mut bytes = [0u8; 64];
            unsafe { libc::read(wakeup.as_raw_fd(), bytes.as_mut_ptr().cast(), bytes.len()) };
        }
        let descriptor = &descriptors[0];
        if descriptor.revents == 0 {
            continue;
        }
        if descriptor.revents & libc::POLLNVAL != 0 {
            return Err("R worker stdin descriptor is invalid".to_string());
        }
        if descriptor.revents & (libc::POLLIN | libc::POLLHUP) == 0 {
            return Err(format!(
                "R worker stdin poll returned unexpected events {}",
                descriptor.revents
            ));
        }
        if interrupted() {
            return cancel_console_stdin_read(buf, length);
        }
        let byte = unsafe { buf.add(length) };
        let count = unsafe { libc::read(libc::STDIN_FILENO, byte.cast(), 1) };
        if count == 1 {
            length += 1;
            if unsafe { *byte } == b'\n' {
                break;
            }
            continue;
        }
        if count == 0 {
            core::mark_shutting_down();
            return Ok(console_eof(buf));
        }

        let error = io::Error::last_os_error();
        if error.kind() == io::ErrorKind::Interrupted {
            continue;
        }
        return Err(format!("R worker stdin read failed: {error}"));
    }
    unsafe {
        *buf.add(length) = 0;
    }
    record_console_stdin_chunk(buf, length)?;
    Ok(length as c_int)
}

fn record_console_stdin_chunk(buf: *const c_uchar, length: usize) -> Result<(), String> {
    let chunk = unsafe { std::slice::from_raw_parts(buf, length) };
    let mut stdin = CONSOLE_STDIN
        .lock()
        .map_err(|_| "R worker console stdin lock poisoned".to_string())?;
    stdin.record_chunk(chunk);
    Ok(())
}

fn cancel_console_stdin_read(buf: *const c_uchar, length: usize) -> Result<c_int, String> {
    let chunk = unsafe { std::slice::from_raw_parts(buf, length) };
    let mut stdin = CONSOLE_STDIN
        .lock()
        .map_err(|_| "R worker console stdin lock poisoned".to_string())?;
    stdin.preserve_line(chunk);
    Ok(-1)
}

pub(super) fn finish_console_stdin_operation() -> Result<(), String> {
    let mut stdin = CONSOLE_STDIN
        .lock()
        .map_err(|_| "R worker console stdin lock poisoned".to_string())?;
    stdin.finish_operation();
    Ok(())
}

pub(crate) enum PythonInput {
    Line(Vec<u8>),
    Interrupted,
    Eof,
}

pub(crate) fn read_python_input(prompt: &str) -> Result<PythonInput, String> {
    (|| -> Result<PythonInput, String> {
        core::send_input_requested(prompt)?;
        let mut line = Vec::new();
        loop {
            let mut buffer = [0u8; 4096];
            let length = read_console_stdin(
                buffer.as_mut_ptr(),
                buffer.len() as c_int,
                super::interrupt::pending,
            )?;
            if length < 0 {
                core::send_input_cancelled()?;
                return Ok(PythonInput::Interrupted);
            }
            if length == 0 {
                core::send_input_received()?;
                return Ok(PythonInput::Eof);
            }
            line.extend_from_slice(&buffer[..length as usize]);
            if line.last() == Some(&b'\n') {
                line.pop();
                core::send_input_received()?;
                return Ok(PythonInput::Line(line));
            }
        }
    })()
    .inspect_err(|error| core::record_worker_failure(error.clone()))
}
