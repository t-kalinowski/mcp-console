use std::ffi::{OsStr, OsString};
use std::path::Path;
use std::process::{Command, ExitCode};

pub(super) fn run(_command: &[OsString]) -> Result<ExitCode, String> {
    Err("`mcp-console sandbox` is not supported on this operating system".to_string())
}

pub(super) fn worker_command(
    _program: &OsStr,
    _temporary_directory: &Path,
) -> Result<Command, String> {
    Err("sandboxed R sessions are not supported on this operating system".to_string())
}
