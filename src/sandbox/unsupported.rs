use std::ffi::OsString;
use std::process::{Command, ExitCode};

pub(super) fn run(_command: &[OsString]) -> Result<ExitCode, String> {
    Err("`mcp-console sandbox` is not supported on this operating system".to_string())
}

pub(super) struct TemporaryDirectory;

pub(super) fn worker_command() -> Result<(Command, TemporaryDirectory), String> {
    Err("sandboxed R sessions are not supported on this operating system".to_string())
}
