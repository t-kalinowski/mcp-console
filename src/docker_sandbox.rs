//! Docker Sandboxes' local sbx adapter. Docker owns policy and microVM lifetime.
use crate::settings::{Access, Compute, DockerSandbox, Provider, SandboxSettings, Target};
use crate::target_launch::{self, Bootstrap, Protocol, process};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

mod owner;
const PROTOCOL: Protocol = Protocol("Docker Sandbox");
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
    #[derive(Deserialize)]
    struct Environment {
        #[serde(default)]
        environment: std::collections::BTreeMap<String, String>,
        #[serde(default, rename = "inherit_environment")]
        _inherit_environment: bool,
    }
    let environment: Environment = serde_json::from_value(Value::Object(policy.clone()))
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
pub(crate) struct Session {
    target: Target,
    version: String,
    #[serde(skip)]
    blocked: Arc<Mutex<Option<String>>>,
}

impl Session {
    pub fn setup(
        target: Target,
        policy: &SandboxSettings,
        no_sandbox: bool,
        started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
        let cancel = process::Cancel::new(PROTOCOL)?;
        started(crate::resolver::ResolverStopHandle::new(cancel.clone()))?;
        let mut command = Command::new("sbx");
        command.arg("version");
        let bytes = process::run(command, &cancel, Some(Instant::now() + COMMAND_TIMEOUT), process::OutputMode::Data, None)
            .map_err(|error| format!("{error}; install standalone sbx v0.42.1 and complete Docker login and policy setup before starting Console; see docs/DOCKER_SANDBOX.md"))?;
        let version = String::from_utf8(bytes).map_err(|error| error.to_string())?;
        // This first adapter targets a verified CLI contract, not legacy docker sandbox.
        if !version.starts_with("sbx version: v0.42.1 ") {
            return Err(format!(
                "Docker Sandbox requires supported sbx v0.42.1; received {}",
                version.trim()
            ));
        }
        let session = Self {
            target,
            version: version.trim().into(),
            blocked: Arc::default(),
        };
        let (command, request, _) = session.launch(policy, no_sandbox, true)?;
        let bytes = process::run(
            command,
            &cancel,
            Some(Instant::now() + Duration::from_secs(40)),
            process::OutputMode::Data,
            Some(process::OwnerInput {
                bytes: request,
                retirement_grace: Duration::from_secs(20),
            }),
        )?;
        let retirement = target_launch::Retirement::default();
        let mut output =
            target_launch::Output::new(std::io::Cursor::new(bytes), PROTOCOL, retirement.clone());
        let mut unexpected = Vec::new();
        output
            .read_to_end(&mut unexpected)
            .map_err(|error| error.to_string())?;
        retirement.check()?;
        if !unexpected.is_empty() {
            return Err("unexpected Docker Sandbox runtime probe output".into());
        }
        Ok(session)
    }

    pub fn metadata(&self) -> Value {
        let Compute::DockerSandbox(config) = &self.target.compute else {
            unreachable!()
        };
        json!({"transport": self.target.transport, "workspace": self.target.workspace,
            "compute": {"kind": "docker_sandbox", "template": config.template,
                "template_identity": config.template, "mounts": config.mounts, "cli_version": self.version}})
    }

    pub fn launch(
        &self,
        policy: &SandboxSettings,
        no_sandbox: bool,
        probe: bool,
    ) -> Result<(Command, Vec<u8>, String), String> {
        if let Some(error) = &*self
            .blocked
            .lock()
            .map_err(|_| "Docker Sandbox session lock poisoned")?
        {
            return Err(error.clone());
        }
        let name = format!("mcp-console-{}", target_launch::owner::token()?);
        let request = owner::Request {
            session: self.clone(),
            name: name.clone(),
            probe,
            bootstrap: Bootstrap {
                version: target_launch::VERSION,
                build: env!("CARGO_PKG_VERSION").into(),
                workspace: self.target.workspace.clone(),
                policy: policy.clone(),
                writable_roots: Vec::new(),
                no_sandbox,
                provider: Provider::Compute,
                environment: None,
            },
        };
        let mut command = Command::new(std::env::current_exe().map_err(|error| error.to_string())?);
        command.arg("docker-sandbox-owner");
        Ok((command, target_launch::encode(&request)?, name))
    }

    pub fn check_retirement(
        &self,
        retirement: &target_launch::Retirement,
        name: &str,
    ) -> Result<(), String> {
        retirement.check().map_err(|error| {
            let error = format!(
                "Docker Sandbox microVM '{name}': {error}; this session cannot start a replacement"
            );
            self.blocked
                .lock()
                .expect("Docker Sandbox session lock")
                .get_or_insert(error.clone());
            error
        })
    }
}

pub(crate) fn run_owner() -> Result<(), String> {
    owner::run()
}
