use std::ffi::OsString;
use std::path::PathBuf;
use std::process::{Command, ExitCode, Stdio};

#[cfg(any(target_os = "macos", target_os = "linux"))]
mod installation;
#[cfg(any(target_os = "macos", target_os = "linux"))]
mod runner;
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
mod unsupported;

pub fn capture_settings(roots: Vec<PathBuf>) -> Result<crate::settings::SandboxSettings, String> {
    let (source, mut settings) = crate::settings::discover()?;
    settings.writable_roots = roots
        .into_iter()
        .map(resolve_writable_root)
        .collect::<Result<_, _>>()?;
    if let Some(entries) = settings
        .policy
        .get_mut("filesystem")
        .and_then(|filesystem| filesystem.get_mut("entries"))
        .and_then(serde_json::Value::as_array_mut)
    {
        for entry in entries {
            if entry
                .pointer("/path/type")
                .and_then(serde_json::Value::as_str)
                == Some("path")
                && let Some(serde_json::Value::String(path)) = entry.pointer_mut("/path/path")
            {
                *path = resolve_writable_root(PathBuf::from(&*path))?
                    .to_string_lossy()
                    .into_owned();
            }
        }
    }
    if let Some(source) = source {
        preflight(&settings).map_err(|error| format!("{source}: {error}"))?;
    }
    Ok(settings)
}

/// Validate native policy and setup before workload startup or server readiness.
/// The child explicitly consumes the snapshot, so it cannot rediscover settings.
fn preflight(settings: &crate::settings::SandboxSettings) -> Result<(), String> {
    let executable = std::env::current_exe()
        .map_err(|error| format!("cannot locate sandbox launcher: {error}"))?;
    let payload = serde_json::to_string(settings)
        .map_err(|error| format!("cannot encode sandbox settings: {error}"))?;
    let output = Command::new(executable)
        .args(["sandbox", "--exit-with-parent"])
        .arg(std::process::id().to_string())
        .args([
            "--settings-env",
            crate::settings::ENVIRONMENT,
            "--",
            "/usr/bin/true",
        ])
        .env(crate::settings::ENVIRONMENT, payload)
        .stdin(Stdio::null())
        .output()
        .map_err(|error| format!("cannot start sandbox preflight: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "sandbox preflight failed ({}): {}",
            output.status,
            String::from_utf8_lossy(&output.stderr).trim_end()
        ));
    }
    Ok(())
}

/// Capture launch-relative paths without hiding symlinks from runner validation.
fn resolve_writable_root(path: PathBuf) -> Result<PathBuf, String> {
    let root = std::path::absolute(&path)
        .map_err(|error| format!("cannot resolve writable root '{}': {error}", path.display()))?;
    // The runner configuration carries paths as JSON strings.
    if root.to_str().is_none() {
        return Err(format!(
            "writable root '{}' is not valid UTF-8",
            path.display()
        ));
    }
    Ok(root)
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
