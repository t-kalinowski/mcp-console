use std::collections::VecDeque;
use std::ffi::{c_int, c_uchar};
use std::io;
use std::sync::Mutex;

use super::core;

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
    // SAFETY: r_read_console validated buf and reserved one byte for NUL.
    let mut length = unsafe { stdin.copy_pushback(buf, capacity) };
    drop(stdin);

    while length < capacity {
        if interrupted() {
            return cancel_console_stdin_read(buf, length);
        }
        let byte = unsafe { buf.add(length) };
        #[cfg(unix)]
        let count = {
            let mut descriptor = libc::pollfd {
                fd: libc::STDIN_FILENO,
                events: libc::POLLIN,
                revents: 0,
            };
            let ready = unsafe { libc::poll(&mut descriptor, 1, 10) };
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

            unsafe { libc::read(libc::STDIN_FILENO, byte.cast(), 1) }
        };
        #[cfg(windows)]
        let count = {
            wait_windows_stdin().map_err(|error| format!("R worker stdin wait failed: {error}"))?;
            if interrupted() {
                return cancel_console_stdin_read(buf, length);
            }
            (unsafe { libc::read(0, byte.cast(), 1) }) as isize
        };
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
    Ok(i32::from(length > 0))
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

#[cfg(windows)]
static WINDOWS_STDIN: std::sync::OnceLock<(crate::windows::Event, crate::windows::Event)> =
    std::sync::OnceLock::new();

#[cfg(windows)]
pub(super) fn initialize_windows_stdin(
    interrupt: crate::windows::Event,
) -> Result<(), Box<dyn std::error::Error>> {
    let handle = std::env::var("MCP_CONSOLE_INPUT_READY_HANDLE")?.parse::<usize>()?;
    if handle == 0 || handle == usize::MAX {
        return Err("invalid stdin wakeup handle".into());
    }
    let ready = unsafe { crate::windows::Event::from_inherited(handle as _)? };
    unsafe {
        std::env::remove_var("MCP_CONSOLE_INPUT_READY_HANDLE");
    }
    WINDOWS_STDIN
        .set((ready, interrupt))
        .map_err(|_| "stdin wakeup already initialized")?;
    Ok(())
}

#[cfg(windows)]
fn wait_windows_stdin() -> io::Result<()> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::{ERROR_BROKEN_PIPE, WAIT_OBJECT_0};
    use windows_sys::Win32::System::Threading::{INFINITE, WaitForMultipleObjects};
    let (ready, interrupt) = WINDOWS_STDIN
        .get()
        .ok_or_else(|| io::Error::other("stdin wakeup is unavailable"))?;
    loop {
        // Reset before inspecting the pipe, so a concurrent writer cannot lose
        // the wakeup between the inspection and the blocking wait.
        ready.reset();
        // R's subprocess helpers can clear GetStdHandle; the CRT still owns
        // the inherited descriptor used by R and Python's raw stdin reads.
        match crate::windows::available(unsafe { libc::get_osfhandle(0) } as _) {
            Ok(bytes) if bytes > 0 => return Ok(()),
            Err(error) if error.raw_os_error() == Some(ERROR_BROKEN_PIPE as i32) => return Ok(()),
            Err(error) => return Err(error),
            _ => {}
        }
        let handles = [interrupt.as_raw_handle(), ready.as_raw_handle()];
        match unsafe { WaitForMultipleObjects(2, handles.as_ptr(), 0, INFINITE) } {
            WAIT_OBJECT_0 => return Ok(()),
            result if result == WAIT_OBJECT_0 + 1 => {}
            _ => return Err(io::Error::last_os_error()),
        }
    }
}
