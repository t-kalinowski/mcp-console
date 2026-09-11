//! Select application policy and replace the frontend with the verified runner.

use super::installation;
use std::ffi::OsString;
use std::os::unix::process::CommandExt as _;
use std::process::{Command, ExitCode};

const CONFIGURATION: &str = "MCP_CONSOLE_SANDBOX_CONFIG";

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
    let mut entries = vec![serde_json::json!({
        "path": {"type": "special", "value": {"kind": "root"}},
        "access": "read",
    })];
    entries.extend(settings.writable_roots.iter().map(|root| {
        serde_json::json!({
            "path": {"type": "path", "path": root},
            "access": "write",
        })
    }));
    let mut configuration = serde_json::json!({
        "version": installation::PROTOCOL_VERSION,
        "filesystem": {"kind": "restricted", "entries": entries},
        "network": settings.network,
        "proxy": settings.proxy.as_ref().map(|proxy| serde_json::json!({
            "enabled": proxy.enabled,
            "enableSocks5": proxy.enable_socks5,
            "enableSocks5Udp": false,
            "allowUpstreamProxy": proxy.allow_upstream_proxy,
            "dangerouslyAllowAllUnixSockets": false,
            "mode": proxy.mode,
            "domains": proxy.domains,
            "unixSockets": null,
            "allowLocalBinding": proxy.allow_local_binding,
        })),
        "lifecycle": {
            "parent_pid": parent,
            "sigterm": if parent.is_some() { "retire" } else { "forward" },
            "private_tmp": {"environment": ["TMPDIR"]},
            "cleanup_timeout_ms": 1000,
        },
    });
    if cfg!(target_os = "macos") {
        configuration["macos_seatbelt_profile_extension"] =
            include_str!("policy_extensions.sbpl").into();
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
        runner.env(CONFIGURATION, configuration.to_string());
    } else if config_env != Some(CONFIGURATION) {
        runner.env_remove(CONFIGURATION);
    }
    if config_env != Some("MCP_CONSOLE_SANDBOX") {
        runner.env("MCP_CONSOLE_SANDBOX", "1");
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
