use std::ffi::OsString;
use std::process::ExitCode;

#[cfg(target_os = "macos")]
use std::os::unix::process::CommandExt as _;
#[cfg(target_os = "macos")]
use std::process::{Command, Stdio};
#[cfg(target_os = "macos")]
use std::time::Duration;

#[cfg(target_os = "macos")]
const MANAGER_CLEANUP_TIMEOUT: Duration = Duration::from_secs(1);
#[cfg(target_os = "macos")]
#[path = "sandbox/child.rs"]
mod child;
#[cfg(target_os = "macos")]
mod installation;
#[path = "sandbox/macos.rs"]
mod platform;
#[cfg(target_os = "macos")]
mod process_group;
#[cfg(target_os = "macos")]
mod runner;
#[cfg(target_os = "macos")]
#[path = "sandbox/supervision.rs"]
mod supervision;

#[cfg(not(target_os = "macos"))]
#[path = "sandbox/unsupported.rs"]
mod platform;

#[cfg(target_os = "macos")]
pub fn run(command_line: &[OsString], exit_with_parent: Option<u32>) -> Result<ExitCode, String> {
    let owner = exit_with_parent
        .map(supervision::SandboxOwner::capture)
        .transpose()?;
    let (program, arguments) = command_line
        .split_first()
        .expect("sandbox command must include a program");
    let (mut sandboxed, temporary_directory) = platform::sandboxed_command()?;
    sandboxed
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    supervision::status(sandboxed, temporary_directory, program, arguments, owner)
}

#[cfg(target_os = "macos")]
pub(crate) fn run_manager(
    root_pid: u32,
    cleanup_timeout_millis: u64,
    temporary_directory: std::path::PathBuf,
) -> Result<(), String> {
    supervision::run_manager(root_pid, cleanup_timeout_millis, temporary_directory)
}

#[cfg(target_os = "macos")]
pub(crate) fn run_target(signal_mask: u32, command_line: &[OsString]) -> Result<ExitCode, String> {
    let (program, arguments) = command_line
        .split_first()
        .expect("sandbox target must include a program");
    let result =
        unsafe { libc::pthread_sigmask(libc::SIG_SETMASK, &signal_mask, std::ptr::null_mut()) };
    if result != 0 {
        return Err(format!(
            "failed to restore sandbox target signal mask: {}",
            std::io::Error::from_raw_os_error(result)
        ));
    }

    let error = Command::new(program).args(arguments).exec();
    Err(format!(
        "failed to launch sandbox target `{}`: {error}",
        program.to_string_lossy()
    ))
}

#[cfg(not(target_os = "macos"))]
pub fn run(command_line: &[OsString], _exit_with_parent: Option<u32>) -> Result<ExitCode, String> {
    platform::run(command_line)
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn run_manager(
    _root_pid: u32,
    _cleanup_timeout_millis: u64,
    _temporary_directory: std::path::PathBuf,
) -> Result<(), String> {
    Err("the sandbox manager is currently supported only on macOS".to_string())
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn run_target(
    _signal_mask: u32,
    _command_line: &[OsString],
) -> Result<ExitCode, String> {
    Err("the sandbox target wrapper is currently supported only on macOS".to_string())
}
