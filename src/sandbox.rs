use std::ffi::OsString;
use std::process::ExitCode;

use std::fs::File;
use std::io::Read as _;
use std::os::fd::{FromRawFd as _, OwnedFd};
use std::os::unix::process::CommandExt as _;
use std::process::{Command, Stdio};
use std::time::Duration;

const MANAGER_CLEANUP_TIMEOUT: Duration = Duration::from_secs(1);
const TARGET_GATE_RELEASE: u8 = 1;

#[path = "sandbox/child.rs"]
mod child;
#[path = "sandbox/macos.rs"]
mod platform;
mod process_group;
#[path = "sandbox/supervision.rs"]
mod supervision;

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

pub(crate) fn run_manager(
    root_pid: u32,
    cleanup_timeout_millis: u64,
    temporary_directory: std::path::PathBuf,
) -> Result<(), String> {
    supervision::run_manager(root_pid, cleanup_timeout_millis, temporary_directory)
}

pub(crate) fn run_target(
    gate_descriptor: libc::c_int,
    command_line: &[OsString],
) -> Result<ExitCode, String> {
    let (program, arguments) = command_line
        .split_first()
        .expect("sandbox target must include a program");
    if gate_descriptor <= libc::STDERR_FILENO {
        return Err("sandbox target startup gate descriptor is invalid".to_string());
    }
    loop {
        if unsafe { libc::fcntl(gate_descriptor, libc::F_GETFD) } >= 0 {
            break;
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(format!(
                "sandbox target startup gate descriptor is invalid: {error}"
            ));
        }
    }
    // SAFETY: the host owner transfers this inherited descriptor to the hidden
    // target process and retains no owner for the child-side copy.
    let gate = unsafe { OwnedFd::from_raw_fd(gate_descriptor) };
    let mut gate = File::from(gate);
    let mut release = [0];
    if let Err(error) = gate.read_exact(&mut release) {
        // Closing the owner endpoint before release cancels private startup.
        // The owner reports the startup failure through its public boundary.
        if error.kind() == std::io::ErrorKind::UnexpectedEof {
            return Ok(ExitCode::FAILURE);
        }
        return Err(format!("failed to await sandbox target startup: {error}"));
    }
    if release != [TARGET_GATE_RELEASE] {
        return Err("sandbox target received an invalid startup release".to_string());
    }
    drop(gate);

    let error = Command::new(program).args(arguments).exec();
    Err(format!(
        "failed to launch sandbox target `{}`: {error}",
        program.to_string_lossy()
    ))
}
