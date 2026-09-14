use super::core;
use std::collections::VecDeque;
use std::ffi::{c_int, c_uchar};
use std::io;
use std::sync::Mutex;

static CONSOLE_STDIN: Mutex<ConsoleStdin> = Mutex::new(ConsoleStdin {
    pushback: VecDeque::new(),
    line_prefix: Vec::new(),
});

fn console_eof(buf: *mut c_uchar) -> c_int {
    unsafe {
        *buf = 0;
    }
    0
}

pub(crate) fn read_line(prompt: &str) -> Result<String, String> {
    core::send_input_requested(prompt)?;
    let mut result = Vec::new();
    loop {
        let mut buffer = vec![0u8; 4096];
        let length = match read_console_stdin(buffer.as_mut_ptr(), buffer.len() as c_int)? {
            -1 => {
                core::send_input_cancelled()?;
                return Err("KeyboardInterrupt".into());
            }
            0 => return Err("EOFError".into()),
            length => length as usize,
        };
        result.extend_from_slice(&buffer[..length]);
        if result.last() == Some(&b'\n') {
            core::send_input_received()?;
            result.pop();
            return String::from_utf8(result).map_err(|error| error.to_string());
        }
    }
}

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
            let length = remaining.len().min(capacity - copied);
            unsafe {
                std::ptr::copy_nonoverlapping(remaining.as_ptr(), destination.add(copied), length);
            }
            copied += length;
            chunk.offset += length;
            if chunk.offset == chunk.bytes.len() {
                self.pushback.pop_front();
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

pub(super) fn read_console_stdin(buf: *mut c_uchar, buflen: c_int) -> Result<c_int, String> {
    let capacity = (buflen as usize) - 1;
    if super::interrupt::pending() {
        return cancel_console_stdin_read(buf, 0);
    }
    let mut stdin = CONSOLE_STDIN
        .lock()
        .map_err(|_| "worker console stdin lock poisoned".to_string())?;
    // SAFETY: r_read_console validated buf and reserved one byte for NUL.
    let mut length = unsafe { stdin.copy_pushback(buf, capacity) };
    drop(stdin);

    while length < capacity {
        if super::interrupt::pending() {
            return cancel_console_stdin_read(buf, length);
        }
        let mut descriptor = libc::pollfd {
            fd: libc::STDIN_FILENO,
            events: libc::POLLIN,
            revents: 0,
        };
        let ready = super::interrupt::wait(std::slice::from_mut(&mut descriptor))?;
        if ready == 0 {
            continue;
        }
        if ready < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(format!("worker stdin poll failed: {error}"));
        }
        if super::interrupt::pending() {
            return cancel_console_stdin_read(buf, length);
        }
        if descriptor.revents == 0 {
            continue;
        }
        if descriptor.revents & libc::POLLNVAL != 0 {
            return Err("worker stdin descriptor is invalid".to_string());
        }
        if descriptor.revents & (libc::POLLIN | libc::POLLHUP) == 0 {
            return Err(format!(
                "worker stdin poll returned unexpected events {}",
                descriptor.revents
            ));
        }
        if super::interrupt::pending() {
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
        return Err(format!("worker stdin read failed: {error}"));
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
        .map_err(|_| "worker console stdin lock poisoned".to_string())?;
    stdin.record_chunk(chunk);
    Ok(())
}

fn cancel_console_stdin_read(buf: *const c_uchar, length: usize) -> Result<c_int, String> {
    let chunk = unsafe { std::slice::from_raw_parts(buf, length) };
    let mut stdin = CONSOLE_STDIN
        .lock()
        .map_err(|_| "worker console stdin lock poisoned".to_string())?;
    stdin.preserve_line(chunk);
    Ok(-1)
}

pub(super) fn finish_console_stdin_operation() -> Result<(), String> {
    let mut stdin = CONSOLE_STDIN
        .lock()
        .map_err(|_| "worker console stdin lock poisoned".to_string())?;
    stdin.finish_operation();
    Ok(())
}
