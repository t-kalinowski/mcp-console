use std::ffi::OsString;
use std::process::ExitCode;

#[cfg(target_os = "macos")]
#[path = "sandbox/macos.rs"]
mod platform;

#[cfg(not(target_os = "macos"))]
#[path = "sandbox/unsupported.rs"]
mod platform;

pub fn run(command: &[OsString]) -> Result<ExitCode, String> {
    platform::run(command)
}
