use std::ffi::OsString;
use std::process::ExitCode;

#[cfg(any(target_os = "macos", target_os = "linux"))]
use std::os::unix::process::CommandExt as _;
#[cfg(any(target_os = "macos", target_os = "linux"))]
use std::process::Command;
#[cfg(target_os = "macos")]
use std::process::Stdio;
#[cfg(any(target_os = "macos", target_os = "linux"))]
use std::time::Duration;

#[cfg(any(target_os = "macos", target_os = "linux"))]
const MANAGER_CLEANUP_TIMEOUT: Duration = Duration::from_secs(1);
#[cfg(target_os = "macos")]
#[path = "sandbox/child.rs"]
mod child;
#[cfg(any(target_os = "macos", target_os = "linux"))]
mod installation;
#[cfg(target_os = "linux")]
mod linux;
#[cfg(any(target_os = "macos", target_os = "linux"))]
#[path = "sandbox/command.rs"]
mod platform;
#[cfg(target_os = "macos")]
mod process_group;
#[cfg(any(target_os = "macos", target_os = "linux"))]
mod runner;
#[cfg(target_os = "macos")]
#[path = "sandbox/supervision.rs"]
mod supervision;

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
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

#[cfg(any(target_os = "macos", target_os = "linux"))]
pub(crate) fn run_target(
    signal_mask: u64,
    ignored_signals: u64,
    command_line: &[OsString],
) -> Result<ExitCode, String> {
    let (program, arguments) = command_line
        .split_first()
        .expect("sandbox target must include a program");
    #[cfg(target_os = "linux")]
    {
        // The native helper can retain an outer procfs when the host denies a
        // fresh mount. Require namespace-local procfs before executing user code.
        let process = std::fs::read_link("/proc/self")
            .map_err(|error| format!("failed to inspect sandbox procfs: {error}"))?;
        if process != std::path::Path::new(&std::process::id().to_string()) {
            return Err("Linux sandbox requires procfs mounted for its PID namespace".to_string());
        }
    }
    let mut mask = unsafe { std::mem::zeroed() };
    unsafe { libc::sigemptyset(&mut mask) };
    for signal in 1..=64 {
        // Supervisors need waitable children and observable retirement signals.
        // Restore the target's ignored dispositions before unblocking delivery.
        if ignored_signals & (1 << (signal - 1)) != 0
            && unsafe { libc::signal(signal, libc::SIG_IGN) } == libc::SIG_ERR
        {
            return Err(format!(
                "failed to restore sandbox target signal {signal}: {}",
                std::io::Error::last_os_error()
            ));
        }
        if signal_mask & (1 << (signal - 1)) != 0 {
            unsafe { libc::sigaddset(&mut mask, signal) };
        }
    }
    let result = unsafe { libc::pthread_sigmask(libc::SIG_SETMASK, &mask, std::ptr::null_mut()) };
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

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
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

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub(crate) fn run_target(
    _signal_mask: u64,
    _ignored_signals: u64,
    _command_line: &[OsString],
) -> Result<ExitCode, String> {
    Err("the sandbox target wrapper is currently supported only on macOS".to_string())
}

#[cfg(target_os = "linux")]
pub fn run(command_line: &[OsString], exit_with_parent: Option<u32>) -> Result<ExitCode, String> {
    linux::run(command_line, exit_with_parent)
}
