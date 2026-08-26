use std::ffi::OsString;
use std::process::ExitCode;

#[cfg(target_os = "macos")]
#[path = "sandbox/driver.rs"]
mod driver;

#[cfg(target_os = "macos")]
#[path = "sandbox/macos.rs"]
mod platform;

#[cfg(not(target_os = "macos"))]
#[path = "sandbox/unsupported.rs"]
mod platform;

#[cfg(target_os = "macos")]
pub(crate) use codex_sandbox_api::SandboxStdioMode;
#[cfg(target_os = "macos")]
pub(crate) use driver::SandboxIoCancellation;
#[cfg(target_os = "macos")]
pub(crate) use platform::{
    SandboxRuntime, SandboxedCommand, SandboxedOutput, SandboxedProcess, SandboxedStdin,
};

pub fn run(command_line: &[OsString]) -> Result<ExitCode, String> {
    platform::run(command_line)
}
