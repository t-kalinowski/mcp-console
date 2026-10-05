use std::ffi::OsString;

#[cfg(any(unix, windows))]
mod event_writer;
#[cfg(any(unix, windows))]
mod lifecycle;
#[cfg(any(unix, windows))]
mod routing;
#[cfg(windows)]
mod windows;
#[cfg(windows)]
pub(crate) fn run(command_line: &[OsString]) -> Result<(), String> {
    windows::run(command_line)
}

#[cfg(not(any(unix, windows)))]
pub(crate) fn run(_command_line: &[OsString]) -> Result<(), String> {
    Err("the worker relay is currently supported only on macOS".to_string())
}

#[cfg(unix)]
pub(crate) fn run(command_line: &[OsString]) -> Result<(), String> {
    supervisor::run(command_line)
}

#[cfg(unix)]
mod commands;
#[cfg(unix)]
mod io;
#[cfg(unix)]
mod streams;
#[cfg(unix)]
mod supervisor;
