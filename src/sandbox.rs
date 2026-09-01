use std::ffi::OsString;
use std::process::ExitCode;

#[cfg(target_os = "macos")]
use std::fs::File;
#[cfg(target_os = "macos")]
use std::io::Read as _;
#[cfg(target_os = "macos")]
use std::os::fd::{AsRawFd as _, FromRawFd as _, OwnedFd};
#[cfg(target_os = "macos")]
use std::os::unix::net::UnixStream;
#[cfg(target_os = "macos")]
use std::os::unix::process::CommandExt as _;
#[cfg(target_os = "macos")]
use std::process::{Command, Stdio};

#[cfg(target_os = "macos")]
const TARGET_GATE_RELEASE: u8 = 1;

#[cfg(target_os = "macos")]
#[path = "sandbox/child.rs"]
mod child;
#[cfg(target_os = "macos")]
#[path = "sandbox/command.rs"]
mod command;
#[cfg(target_os = "macos")]
#[path = "sandbox/file_descriptors.rs"]
mod file_descriptors;
#[cfg(target_os = "macos")]
#[path = "sandbox/macos.rs"]
mod platform;
#[cfg(target_os = "macos")]
#[path = "sandbox/supervision.rs"]
mod supervision;
#[cfg(target_os = "macos")]
#[path = "sandbox/spawn.rs"]
mod spawn;

#[cfg(target_os = "macos")]
pub(crate) use child::force_stop_process_group_members_except_self;
#[cfg(target_os = "macos")]
pub(crate) use command::{SandboxedChild, SandboxedCommand};

#[cfg(not(target_os = "macos"))]
#[path = "sandbox/unsupported.rs"]
mod platform;

#[cfg(target_os = "macos")]
pub fn run(command_line: &[OsString]) -> Result<ExitCode, String> {
    let (target_gate, launcher_gate) = UnixStream::pair()
        .map_err(|error| format!("failed to create the sandbox target startup gate: {error}"))?;
    let target_gate_descriptor = target_gate.as_raw_fd();
    let executable = std::env::current_exe()
        .map_err(|error| format!("failed to locate the sandbox target gate: {error}"))?;
    let mut sandboxed = SandboxedCommand::new_direct(executable.as_os_str())?;
    sandboxed
        .arg("sandbox-target")
        .arg("--gate-fd")
        .arg(target_gate_descriptor.to_string())
        .arg("--")
        .args(command_line)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    sandboxed.status(target_gate, launcher_gate)
}

#[cfg(target_os = "macos")]
pub(crate) fn run_manager() -> Result<(), String> {
    supervision::run_manager()
}

#[cfg(target_os = "macos")]
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
    gate.read_exact(&mut release)
        .map_err(|error| format!("failed to await sandbox target startup: {error}"))?;
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

#[cfg(not(target_os = "macos"))]
pub fn run(command_line: &[OsString]) -> Result<ExitCode, String> {
    platform::run(command_line)
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn run_manager() -> Result<(), String> {
    Err("the sandbox manager is currently supported only on macOS".to_string())
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn run_target(
    _gate_descriptor: libc::c_int,
    _command_line: &[OsString],
) -> Result<ExitCode, String> {
    Err("the sandbox target gate is currently supported only on macOS".to_string())
}
