use std::ffi::OsString;
use std::process::{Command, ExitCode};

const UNSUPPORTED: &str = "`mcp-console sandbox` is not supported on this operating system";

pub(super) struct TemporaryDirectory;

pub(super) fn sandboxed_command() -> Result<(Command, TemporaryDirectory, OsString), String> {
    Err(UNSUPPORTED.to_string())
}

pub(super) fn status(_launcher: &mut Command) -> Result<ExitCode, String> {
    Err(UNSUPPORTED.to_string())
}
