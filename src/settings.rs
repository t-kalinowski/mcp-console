//! Trusted application settings, captured before starting a session or workload.

use std::ffi::OsStr;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

mod yaml;

pub const ENVIRONMENT: &str = "MCP_CONSOLE_SANDBOX_SETTINGS";

/// Native policy values; application additions materialize on the execution host.
pub type SandboxSettings = Map<String, Value>;

/// Preserve Console's assignments and removals after project environment controls.
pub fn preserve_environment<'a>(
    policy: &mut SandboxSettings,
    values: impl IntoIterator<Item = (&'a OsStr, Option<&'a OsStr>)>,
) -> Result<(), String> {
    let inherit = policy.get("inherit_environment") != Some(&Value::Bool(false));
    if !inherit {
        policy
            .entry("environment")
            .or_insert_with(|| Value::Object(Map::new()));
    }
    if let Some(Value::Object(environment)) = policy.get_mut("environment") {
        for (name, value) in values {
            let name = name
                .to_str()
                .ok_or_else(|| "worker environment name must be UTF-8".to_string())?;
            // Keep invalid native values for execution-host validation, even
            // when Console owns the assignment or removal of a valid value.
            if environment
                .get(name)
                .is_some_and(|value| !value.is_string())
            {
                continue;
            }
            if !inherit && let Some(value) = value {
                let value = value.to_str().ok_or_else(|| {
                    format!("worker environment value for '{name}' must be UTF-8")
                })?;
                environment.insert(name.into(), value.into());
            } else {
                environment.remove(name);
            }
        }
    }
    Ok(())
}

/// Recognize native unit variants for application additions and descriptions.
/// This does not validate or transform policy values sent to the runner.
pub fn native_variant_name(value: &Value) -> Option<&str> {
    match value {
        Value::String(name) => Some(name),
        Value::Object(object) if object.len() == 1 => object
            .iter()
            .next()
            .and_then(|(name, value)| value.is_null().then_some(name.as_str())),
        _ => None,
    }
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Project {
    extends: Option<String>,
    sandbox: Map<String, Value>,
    target: Option<SshTarget>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SshTarget {
    pub transport: Transport,
    #[serde(default)]
    pub workspace: String,
    #[serde(default = "remote_command")]
    pub command: Vec<String>,
    #[serde(default = "ssh_lease_ms")]
    pub lease_ms: u64,
}

fn ssh_lease_ms() -> u64 {
    30_000
}

pub(crate) fn validate_ssh_lease(lease_ms: u64) -> Result<(), String> {
    if !(1_000..=300_000).contains(&lease_ms) {
        return Err(
            "target.lease_ms must be an integer from 1000 through 300000 milliseconds".into(),
        );
    }
    Ok(())
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum Transport {
    Ssh { host: String },
}

fn remote_command() -> Vec<String> {
    vec!["uvx".into(), "mcp-console".into()]
}

impl SshTarget {
    pub fn host(&self) -> &str {
        let Transport::Ssh { host } = &self.transport;
        host
    }

    fn validate(&self) -> Result<(), String> {
        validate_ssh_lease(self.lease_ms)?;
        if !self.workspace.starts_with('/') || self.workspace.contains('\0') {
            return Err("target.workspace must be an absolute remote directory path".into());
        }
        if self.host().is_empty() || self.host().contains('\0') {
            return Err("target.transport.host must be a nonempty SSH destination".into());
        }
        if self.command.is_empty()
            || self.command[0].is_empty()
            || self.command.iter().any(|argument| argument.contains('\0'))
        {
            return Err("target.command must be a nonempty argv with a nonempty executable and no NUL bytes".into());
        }
        Ok(())
    }
}

pub fn discover() -> Result<(Option<&'static str>, SandboxSettings, Option<SshTarget>), String> {
    let name = ".agents/console/config.yaml";
    // A dangling symlink or an unreadable existing file must reach read_to_string.
    match std::fs::symlink_metadata(name) {
        // No configuration file can exist below a non-directory component.
        Err(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
            ) =>
        {
            return Ok((None, SandboxSettings::default(), None));
        }
        Err(error) => return Err(format!("cannot inspect '{name}': {error}")),
        Ok(_) => {}
    }
    let source =
        std::fs::read_to_string(name).map_err(|error| format!("cannot read '{name}': {error}"))?;
    let value = yaml::load(&source).map_err(|error| format!("{name}: {error}"))?;
    let mut project: Project =
        serde_path_to_error::deserialize(value).map_err(|error| format!("{name}: {error}"))?;
    // These fields belong to Console's launch protocol and worker lifetime.
    // All other sandbox fields and values are interpreted by the native runner.
    for field in ["version", "lifecycle", "extends", "workspace"] {
        if project.sandbox.contains_key(field) {
            return Err(format!("{name}: sandbox.{field} is managed by Console"));
        }
    }
    if let Some(profile) = project.extends {
        project.sandbox.insert("extends".into(), profile.into());
    }
    if let Some(target) = &project.target {
        target
            .validate()
            .map_err(|error| format!("{name}: {error}"))?;
    }
    Ok((Some(name), project.sandbox, project.target))
}

pub fn from_environment(name: &str) -> Result<SandboxSettings, String> {
    let value = std::env::var(name)
        .map_err(|error| format!("cannot read sandbox settings environment '{name}': {error}"))?;
    serde_json::from_str(&value)
        .map_err(|error| format!("invalid sandbox settings environment '{name}': {error}"))
}
