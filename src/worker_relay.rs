use std::ffi::OsString;

#[cfg(unix)]
mod event_writer;

#[cfg(not(unix))]
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
