use std::ffi::{OsStr, OsString};
use std::process::ExitCode;

#[cfg(target_os = "macos")]
mod macos;
#[cfg(target_os = "macos")]
use self::macos as platform;

#[cfg(not(target_os = "macos"))]
mod unsupported;
#[cfg(not(target_os = "macos"))]
use self::unsupported as platform;

pub fn run(command: &[OsString]) -> Result<ExitCode, String> {
    let command = match command {
        [separator, command @ ..] if separator == OsStr::new("--") => command,
        command => command,
    };

    if command.is_empty() {
        return Err("usage: mcp-console sandbox [--] COMMAND [ARG]...".to_string());
    }

    platform::run(command)
}
