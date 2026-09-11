use std::ffi::OsString;
use std::path::PathBuf;
use std::process::ExitCode;

#[cfg(any(target_os = "macos", target_os = "linux"))]
mod installation;
#[cfg(any(target_os = "macos", target_os = "linux"))]
mod runner;
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
mod unsupported;

pub fn capture_settings(roots: Vec<PathBuf>) -> Result<crate::settings::SandboxSettings, String> {
    let mut settings = crate::settings::discover()?;
    settings.writable_roots.extend(roots);
    settings.writable_roots = resolve_writable_roots(settings.writable_roots)?;
    Ok(settings)
}

/// Capture launch-relative paths without hiding symlinks from runner validation.
pub fn resolve_writable_roots(roots: Vec<PathBuf>) -> Result<Vec<PathBuf>, String> {
    roots
        .into_iter()
        .map(|path| {
            let root = std::path::absolute(&path).map_err(|error| {
                format!("cannot resolve writable root '{}': {error}", path.display())
            })?;
            // The runner configuration carries paths as JSON strings.
            if root.to_str().is_none() {
                return Err(format!(
                    "writable root '{}' is not valid UTF-8",
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
    settings_env: Option<&str>,
    writable_roots: Vec<PathBuf>,
) -> Result<ExitCode, String> {
    let settings = if config_env.is_some() {
        crate::settings::SandboxSettings::default()
    } else if let Some(name) = settings_env {
        crate::settings::from_environment(name)?
    } else {
        capture_settings(writable_roots)?
    };
    #[cfg(any(target_os = "macos", target_os = "linux"))]
    {
        runner::run(
            command,
            exit_with_parent,
            config_env,
            settings_env,
            &settings,
        )
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        let _ = (exit_with_parent, config_env, settings_env, settings);
        unsupported::run(command)
    }
}
