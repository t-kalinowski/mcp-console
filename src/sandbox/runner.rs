//! Apply launch requirements and replace the frontend with the verified runner.

use super::{MARKER, installation};
use crate::settings::SandboxSettings;
use serde_json::{Value, json};
use std::ffi::OsString;
use std::os::unix::process::CommandExt as _;
use std::process::{Command, ExitCode};

const CONFIGURATION: &str = "MCP_CONSOLE_SANDBOX_CONFIG";

pub(super) fn run(
    command: &[OsString],
    parent: Option<u32>,
    config_env: Option<&str>,
    settings_env: Option<&str>,
    mut settings: SandboxSettings,
) -> Result<ExitCode, String> {
    if let Some(pid) = parent
        && unsafe { libc::getppid() } as u32 != pid
    {
        return Err(format!(
            "sandbox owner {pid} is not the launcher's current parent"
        ));
    }
    // The runner captures and monitors this same caller after exec. No waiting
    // adapter changes its direct-parent identity or retains a standard stream.
    let mut runner = Command::new(installation::private_runner()?);
    if let Some(name) = settings_env {
        runner.env_remove(name);
    }
    if config_env != Some(crate::settings::ENVIRONMENT) {
        runner.env_remove(crate::settings::ENVIRONMENT);
    }
    if config_env.is_none() {
        // This is also the serve path: an ambient value never selects policy.
        settings.insert("version".into(), installation::PROTOCOL_VERSION.into());
        let mut lifecycle = json!({"private_tmp": {"environment": ["TMPDIR"]}});
        if let Some(parent) = parent {
            lifecycle["parent_pid"] = parent.into();
            lifecycle["sigterm"] = "retire".into();
        }
        settings.insert("lifecycle".into(), lifecycle);
        runner.env(CONFIGURATION, Value::Object(settings).to_string());
    } else if config_env != Some(CONFIGURATION) {
        runner.env_remove(CONFIGURATION);
    }
    if config_env != Some(MARKER) {
        runner.env(MARKER, "1");
    }
    let error = runner
        .args(["--config-env", config_env.unwrap_or(CONFIGURATION), "--"])
        .args(command)
        .env_remove("DYLD_INSERT_LIBRARIES")
        .env_remove("LD_PRELOAD")
        .exec();
    let detail = if error.raw_os_error() == Some(libc::E2BIG) {
        "argument/environment size limit exceeded (E2BIG); reduce the launch environment or use the private runner descriptor transport".to_owned()
    } else {
        error.to_string()
    };
    Err(format!("failed to launch private sandbox runner: {detail}"))
}
