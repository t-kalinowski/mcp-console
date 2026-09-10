use std::ffi::OsString;
use std::path::PathBuf;
use std::process::ExitCode;

#[cfg(any(target_os = "macos", target_os = "linux"))]
mod installation;
#[cfg(any(target_os = "macos", target_os = "linux"))]
mod runner;
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
mod unsupported;

/// Capture launch-relative paths without hiding symlinks from runner validation.
pub fn resolve_writable_roots(roots: Vec<PathBuf>) -> Result<Vec<PathBuf>, String> {
    roots
        .into_iter()
        .map(|path| {
            let root = std::path::absolute(&path).map_err(|error| {
                format!("cannot resolve writable root '{}': {error}", path.display())
            })?;
            let metadata = root.metadata().map_err(|error| {
                format!("cannot access writable root '{}': {error}", path.display())
            })?;
            if !metadata.is_dir() {
                return Err(format!(
                    "writable root '{}' is not a directory",
                    path.display()
                ));
            }
            Ok(root)
        })
        .collect()
}

pub fn run(
    command: &[OsString],
    exit_with_parent: Option<u32>,
    config_env: Option<&str>,
    writable_roots: Vec<PathBuf>,
) -> Result<ExitCode, String> {
    let writable_roots = resolve_writable_roots(writable_roots)?;
    #[cfg(any(target_os = "macos", target_os = "linux"))]
    {
        runner::run(command, exit_with_parent, config_env, &writable_roots)
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        let _ = (exit_with_parent, config_env, writable_roots);
        unsupported::run(command)
    }
}
