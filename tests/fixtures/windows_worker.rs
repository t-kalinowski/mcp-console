//! Deterministic worker endpoints for Windows relay protocol regressions.
use std::ffi::c_void;
use std::io::{self, Read, Write};
use std::net::TcpStream;
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};

#[repr(C)]
struct Overlapped {
    internal: usize,
    internal_high: usize,
    offset: u32,
    offset_high: u32,
    event: *mut c_void,
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn CreateEventW(a: *const c_void, manual: i32, initial: i32, name: *const u16) -> *mut c_void;
    fn ReadFile(h: *mut c_void, b: *mut u8, n: u32, count: *mut u32, o: *mut Overlapped) -> i32;
    fn WriteFile(h: *mut c_void, b: *const u8, n: u32, count: *mut u32, o: *mut Overlapped) -> i32;
    fn GetOverlappedResult(h: *mut c_void, o: *mut Overlapped, count: *mut u32, wait: i32) -> i32;
}
unsafe extern "C" {
    fn _close(fd: i32) -> i32;
}

fn transfer(handle: *mut c_void, bytes: &mut [u8], write: bool) -> io::Result<usize> {
    let event = unsafe { CreateEventW(std::ptr::null(), 1, 0, std::ptr::null()) };
    if event.is_null() {
        return Err(io::Error::last_os_error());
    }
    let event = unsafe { OwnedHandle::from_raw_handle(event) };
    let mut overlapped: Overlapped = unsafe { std::mem::zeroed() };
    overlapped.event = event.as_raw_handle();
    let started = unsafe {
        if write {
            WriteFile(
                handle,
                bytes.as_ptr(),
                bytes.len() as u32,
                std::ptr::null_mut(),
                &mut overlapped,
            )
        } else {
            ReadFile(
                handle,
                bytes.as_mut_ptr(),
                bytes.len() as u32,
                std::ptr::null_mut(),
                &mut overlapped,
            )
        }
    };
    if started == 0 && io::Error::last_os_error().raw_os_error() != Some(997) {
        return Err(io::Error::last_os_error());
    }
    let mut count = 0;
    if unsafe { GetOverlappedResult(handle, &mut overlapped, &mut count, 1) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(count as usize)
}

fn send(handle: *mut c_void, text: &str) -> io::Result<()> {
    let mut bytes = text.as_bytes().to_vec();
    let mut offset = 0;
    while offset < bytes.len() {
        offset += transfer(handle, &mut bytes[offset..], true)?;
    }
    Ok(())
}

fn main() -> io::Result<()> {
    let handle = |name| std::env::var(name).unwrap().parse::<usize>().unwrap() as *mut c_void;
    let read = handle("MCP_CONSOLE_SIDEBAND_READ_HANDLE");
    let write = handle("MCP_CONSOLE_SIDEBAND_WRITE_HANDLE");
    let scenario = std::env::var("TEST_WORKER_SCENARIO").unwrap();
    let mut ready = TcpStream::connect(std::env::var("TEST_WORKER_READY").unwrap())?;
    writeln!(ready, "{}", std::process::id())?;
    drop(ready);
    if scenario == "closed_stdin" {
        assert_eq!(unsafe { _close(0) }, 0);
    } else if scenario == "commands" {
        std::thread::spawn(|| {
            let mut byte = [0];
            if io::stdin().read_exact(&mut byte).is_ok() {
                std::fs::write(std::env::var("TEST_DISPATCHED").unwrap(), b"stdin").unwrap();
            }
        });
    }
    send(write, "{\"kind\":\"ready\"}\n")?;
    loop {
        let mut command = Vec::new();
        loop {
            let mut byte = [0];
            transfer(read, &mut byte, false)?;
            command.push(byte[0]);
            if byte[0] == b'\n' {
                break;
            }
        }
        let command = String::from_utf8(command).unwrap();
        if command.contains("shutdown") {
            return Ok(());
        }
        match scenario.as_str() {
            "commands" => std::fs::write(std::env::var("TEST_DISPATCHED").unwrap(), command)?,
            "invalid_sideband" => send(write, "{\"kind\":\"broken\"}\n")?,
            "exit_tail" | "exit_invalid" => {
                let mut frames = String::new();
                for index in 0..1024 {
                    frames.push_str(&format!(
                        "{{\"kind\":\"console_output\",\"data\":\"{index:04}\"}}\n"
                    ));
                }
                if scenario == "exit_invalid" {
                    frames.push_str("{\"kind\":\"broken\"}\n");
                } else {
                    frames.push_str(
                        "{\"kind\":\"image\",\"data\":\"eA==\",\"mime_type\":\"image/png\"}\n",
                    );
                    frames.push_str("{\"kind\":\"completed\"}\n");
                }
                send(write, &frames)?;
                return Ok(());
            }
            _ => {}
        }
    }
}
