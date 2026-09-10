use std::ffi::OsString;
use std::process::ExitCode;

#[cfg(any(target_os = "macos", target_os = "linux"))]
mod installation;
#[cfg(any(target_os = "macos", target_os = "linux"))]
mod runner;
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
mod unsupported;

pub fn run(
    command: &[OsString],
    exit_with_parent: Option<u32>,
    config_env: Option<&str>,
) -> Result<ExitCode, String> {
    #[cfg(any(target_os = "macos", target_os = "linux"))]
    {
        runner::run(command, exit_with_parent, config_env)
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        let _ = (exit_with_parent, config_env);
        unsupported::run(command)
    }
}
