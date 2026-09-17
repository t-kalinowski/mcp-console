//! Docker Sandboxes' local sbx adapter. Docker owns policy and microVM lifetime.
#[cfg(unix)]
use crate::settings::Access;
use crate::settings::{Compute, DockerSandbox, SandboxSettings, Target};
use crate::target_launch::{self, Protocol, process};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

#[cfg(unix)]
mod owner;
pub(crate) const PROTOCOL: Protocol = Protocol("Docker Sandbox");
pub(crate) const PROFILE: crate::target_session::ComputeProfile =
    crate::target_session::ComputeProfile {
        protocol: PROTOCOL,
        resource: "microVM",
        owner_command: "docker-sandbox-owner",
        probe_output: process::OutputMode::Data,
        probe_retirement_grace: Duration::from_secs(20),
        retirement_grace: Duration::from_secs(20),
    };

const COMMAND_TIMEOUT: Duration = Duration::from_secs(10);

pub(crate) fn validate_policy(
    policy: &SandboxSettings,
    extends: bool,
    roots: &[PathBuf],
) -> Result<(), String> {
    let field = if extends {
        Some("extends".into())
    } else if !roots.is_empty() {
        Some("--writable-root".into())
    } else {
        policy
            .keys()
            .find(|key| !matches!(key.as_str(), "environment" | "inherit_environment"))
            .map(|key| format!("sandbox.{key}"))
    };
    if let Some(field) = field {
        return Err(format!(
            "Docker Sandbox compute provider does not support {field}; use Docker Sandbox policy and target.compute.mounts shared-path configuration"
        ));
    }
    // These are workload controls, not provider/daemon configuration.
    let environment = target_launch::WorkloadEnvironment::from_policy(policy)
        .map_err(|error| format!("invalid Docker Sandbox workload environment: {error}"))?;
    for (name, value) in environment.environment {
        if name.is_empty() || name.contains(['=', '\0']) || value.contains('\0') {
            return Err("Docker Sandbox environment names must be nonempty and contain neither '=' nor NUL; values must contain no NUL".into());
        }
    }
    Ok(())
}

pub(crate) fn capture(config: &mut DockerSandbox) -> Result<(), String> {
    let valid = config
        .template
        .split_once("@sha256:")
        .is_some_and(|(repository, digest)| {
            !repository.is_empty()
                && repository.contains('/')
                && !repository.chars().any(char::is_whitespace)
                && digest.len() == 64
                && digest.bytes().all(|byte| byte.is_ascii_hexdigit())
        });
    if !valid || config.template.contains('\0') {
        return Err("Docker Sandbox target.compute.template requires a digest-qualified registry reference (registry/repository@sha256:<64 hex digits>); sbx local template listings do not supply an immutable full identity".into());
    }
    for mount in &mut config.mounts {
        let source = mount
            .source
            .to_str()
            .ok_or("Docker Sandbox shared paths must be UTF-8")?;
        if source.is_empty() || source.contains(['\0', ':']) {
            return Err("Docker Sandbox shared sources must be nonempty and contain neither NUL nor ':' (reserved for :ro)".into());
        }
        let absolute = std::path::absolute(&mount.source).map_err(|error| error.to_string())?;
        let mut resolved = PathBuf::new();
        for component in absolute.components() {
            if component == std::path::Component::ParentDir {
                resolved.pop();
            } else {
                resolved.push(component);
            }
        }
        mount.source = resolved;
        if Path::new(&mount.target) != mount.source {
            return Err("Docker Sandbox shares use the same absolute path on host and guest; mounts[].target must equal the resolved source (remapping is unsupported)".into());
        }
    }
    Ok(())
}

#[derive(Clone, Deserialize, Serialize)]
pub(crate) struct Captured {
    pub target: Target,
    version: String,
}

impl Captured {
    pub fn capture(target: Target, cancel: &process::Cancel) -> Result<Self, String> {
        let mut command = Command::new("sbx");
        command.arg("version");
        let bytes = process::run(command, cancel, Some(Instant::now() + COMMAND_TIMEOUT), process::OutputMode::Data, None)
            .map_err(|error| format!("{error}; install standalone sbx v0.42.1 and complete Docker login and policy setup before starting Console; see docs/DOCKER_SANDBOX.md"))?;
        let version = String::from_utf8(bytes).map_err(|error| error.to_string())?;
        // This first adapter targets a verified CLI contract, not legacy docker sandbox.
        if !version.starts_with("sbx version: v0.42.1 ") {
            return Err(format!(
                "Docker Sandbox requires supported sbx v0.42.1; received {}",
                version.trim()
            ));
        }
        Ok(Self {
            target,
            version: version.trim().into(),
        })
    }

    pub fn metadata(&self) -> Value {
        let Compute::DockerSandbox(config) = &self.target.compute else {
            unreachable!()
        };
        json!({"transport": self.target.transport, "workspace": self.target.workspace,
            "compute": {"kind": "docker_sandbox", "template": config.template,
                "template_identity": config.template, "mounts": config.mounts, "cli_version": self.version}})
    }
}

pub(crate) fn run_owner() -> Result<(), String> {
    #[cfg(unix)]
    return owner::run();
    #[cfg(not(unix))]
    Err("Windows currently supports local execution only".into())
}
