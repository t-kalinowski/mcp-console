use std::ffi::OsString;
use std::process::ExitCode;

#[cfg(target_os = "macos")]
#[path = "sandbox/job_control.rs"]
mod job_control;
#[cfg(any(target_os = "macos", target_os = "linux"))]
#[path = "sandbox/protocol.rs"]
mod protocol;
#[cfg(any(target_os = "macos", target_os = "linux"))]
#[path = "sandbox/runner.rs"]
mod runner;
#[cfg(any(target_os = "macos", target_os = "linux"))]
#[path = "sandbox/temporary_directory.rs"]
mod temporary_directory;

#[cfg(any(target_os = "macos", target_os = "linux"))]
pub(crate) use runner::{SandboxedChild, SandboxedCommand};

#[cfg(target_os = "macos")]
pub fn run(command_line: &[OsString]) -> Result<ExitCode, String> {
    let (program, arguments) = command_line
        .split_first()
        .expect("sandbox command must include a program");
    let signal_relay = job_control::SignalRelay::install()?;
    let mut command = SandboxedCommand::command(program)?;
    command
        .args(arguments)
        .stdin_inherited()
        .stdout_inherited()
        .stderr_inherited()
        .restore_signal_mask(signal_relay.inherited_mask());
    let mut child = command.spawn()?;
    loop {
        let pending = match signal_relay.pending() {
            Ok(pending) => pending,
            Err(error) => return Err(stop_after_error(&mut child, error)),
        };
        for signal in pending {
            if let Err(error) = child.forward_signal(signal) {
                return Err(stop_after_error(&mut child, error));
            }
        }
        match child.wait_timeout_without_reaping(std::time::Duration::from_millis(100)) {
            Ok(true) => break,
            Ok(false) => {}
            Err(error) => return Err(stop_after_error(&mut child, error)),
        }
    }
    child.finish_exit_code()
}

#[cfg(target_os = "linux")]
pub fn run(command_line: &[OsString]) -> Result<ExitCode, String> {
    let (program, arguments) = command_line
        .split_first()
        .expect("sandbox command must include a program");
    let mut command = SandboxedCommand::command(program)?;
    command
        .args(arguments)
        .stdin_inherited()
        .stdout_inherited()
        .stderr_inherited();
    let mut child = command.spawn()?;
    loop {
        match child.wait_timeout_without_reaping(std::time::Duration::from_millis(100)) {
            Ok(true) => break,
            Ok(false) => {}
            Err(error) => return Err(stop_after_error(&mut child, error)),
        }
    }
    child.finish_exit_code()
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub fn run(_command_line: &[OsString]) -> Result<ExitCode, String> {
    Err("sandbox execution is unavailable on this platform".to_string())
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
fn stop_after_error(child: &mut SandboxedChild, error: String) -> String {
    let stop = child.force_stop();
    [Some(error), stop.err()]
        .into_iter()
        .flatten()
        .collect::<Vec<_>>()
        .join("; additionally ")
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
pub(crate) fn configure_child_reaping() -> Result<(), String> {
    // SAFETY: a zeroed sigaction is a valid starting value before every field
    // used by sigaction(2) is initialized below.
    let mut action = unsafe { std::mem::zeroed::<libc::sigaction>() };
    action.sa_sigaction = libc::SIG_DFL;
    // SAFETY: action.sa_mask points to initialized writable storage.
    unsafe { libc::sigemptyset(&mut action.sa_mask) };
    // SAFETY: action is fully initialized and the old action is not requested.
    if unsafe { libc::sigaction(libc::SIGCHLD, &action, std::ptr::null_mut()) } == 0 {
        Ok(())
    } else {
        Err(format!(
            "failed to configure child-process reaping: {}",
            std::io::Error::last_os_error()
        ))
    }
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub(crate) fn configure_child_reaping() -> Result<(), String> {
    Ok(())
}
