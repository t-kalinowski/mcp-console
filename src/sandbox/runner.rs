//! Select application policy and replace the frontend with the verified runner.

use super::installation;
use std::ffi::OsString;
use std::os::unix::process::CommandExt as _;
use std::process::{Command, ExitCode};

const CONFIGURATION: &str = "MCP_CONSOLE_SANDBOX_CONFIG";
const MARKER: &str = "MCP_CONSOLE_SANDBOX";

pub(super) fn run(
    command: &[OsString],
    parent: Option<u32>,
    config_env: Option<&str>,
    settings_env: Option<&str>,
    settings: &crate::settings::SandboxSettings,
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
    let mut configuration = settings.policy.clone();
    let filesystem = configuration
        .entry("filesystem")
        .or_insert_with(|| serde_json::json!({}));
    // Only augment the known application policy. Other shapes and kinds reach
    // the runner unchanged, including values that it may reject.
    let mut restricted = false;
    if let Some(filesystem) = filesystem.as_object_mut() {
        restricted = crate::settings::native_variant_name(
            filesystem
                .entry("kind")
                .or_insert_with(|| "restricted".into()),
        ) == Some("restricted");
        if restricted || !settings.writable_roots.is_empty() {
            let entries = filesystem
                .entry("entries")
                .or_insert_with(|| serde_json::json!([]));
            if let Some(entries) = entries.as_array_mut() {
                if restricted {
                    entries.insert(
                        0,
                        serde_json::json!({
                            "path": {"type": "special", "value": {"kind": "root"}},
                            "access": "read",
                        }),
                    );
                }
                entries.extend(settings.writable_roots.iter().map(|root| {
                    serde_json::json!({
                        "path": {"type": "path", "path": root},
                        "access": "write",
                    })
                }));
            }
        }
    }
    configuration
        .entry("network")
        .or_insert_with(|| "restricted".into());
    if configuration
        .get("proxy")
        .is_some_and(serde_json::Value::is_null)
    {
        configuration.remove("proxy");
    }
    configuration.insert("version".into(), installation::PROTOCOL_VERSION.into());
    let mut lifecycle = serde_json::json!({"private_tmp": {"environment": ["TMPDIR"]}});
    if let Some(parent) = parent {
        lifecycle["parent_pid"] = parent.into();
        lifecycle["sigterm"] = "retire".into();
    }
    configuration.insert("lifecycle".into(), lifecycle);
    if cfg!(target_os = "macos") && restricted {
        configuration
            .entry("macos_seatbelt_profile_extension")
            .or_insert_with(|| include_str!("policy_extensions.sbpl").into());
    }
    let mut runner = Command::new(installation::private_runner()?);
    if let Some(name) = settings_env {
        runner.env_remove(name);
    }
    if config_env != Some(crate::settings::ENVIRONMENT) {
        runner.env_remove(crate::settings::ENVIRONMENT);
    }
    if config_env.is_none() {
        // This is also the serve path: an ambient value never selects policy.
        let inherit_environment =
            configuration.get("inherit_environment") != Some(&serde_json::Value::Bool(false));
        if (!inherit_environment || configuration.contains_key("environment"))
            && let Some(environment) = configuration
                .entry("environment")
                .or_insert_with(|| serde_json::json!({}))
                .as_object_mut()
        {
            // The runtime uses this application marker after native setup.
            // Inherited launches already receive it from the runner environment.
            if inherit_environment {
                environment.remove(MARKER);
            } else {
                environment.insert(MARKER.into(), "1".into());
            }
        }
        runner.env(
            CONFIGURATION,
            serde_json::Value::Object(configuration).to_string(),
        );
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
