//! Trusted application settings, captured before starting a session or workload.

use std::ffi::OsStr;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

mod target;
pub(crate) use target::{Access, Compute, DockerSandbox, Pull, Target};

/// Selected enforcement, independently of direct versus inner-runner launch.
#[derive(Clone, Copy, Default, PartialEq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Provider {
    #[default]
    Native,
    Compute,
}

impl Provider {
    pub fn needs_native_runner(self, no_sandbox: bool) -> bool {
        self == Self::Native && !no_sandbox
    }
}

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
    target: Option<Target>,
}

#[derive(Default)]
pub(crate) struct Captured {
    pub source: Option<String>,
    pub policy: SandboxSettings,
    pub target: Option<Target>,
    pub provider: Provider,
}

pub fn discover(overrides: &[String]) -> Result<Captured, String> {
    let project = Path::new(".agents/console/config.yaml");
    let path = match std::fs::symlink_metadata(project) {
        Ok(_) => Some(PathBuf::from(project)),
        Err(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
            ) =>
        {
            crate::console_paths::home_console_directory()?
                .map(|directory| directory.join("config.yaml"))
        }
        Err(error) => return Err(format!("cannot inspect '{}': {error}", project.display())),
    };
    let Some(value) = crate::config::load(path.as_deref(), overrides)? else {
        return Ok(Captured::default());
    };
    let name = if overrides.is_empty() {
        path.expect("configuration came from a file")
            .to_string_lossy()
            .into_owned()
    } else {
        "configuration with CLI overrides".into()
    };
    let has_extends = value.get("extends").is_some();
    let mut project: Project =
        serde_path_to_error::deserialize(value).map_err(|error| format!("{name}: {error}"))?;
    let compute = project
        .target
        .as_ref()
        .is_some_and(|target| matches!(target.compute, Compute::DockerSandbox(_)));
    let provider = match project.sandbox.remove("provider") {
        Some(value) => serde_json::from_value(value)
            .map_err(|error| format!("{name}: sandbox.provider: {error}"))?,
        None if compute => Provider::Compute,
        None => Provider::Native,
    };
    if provider == Provider::Compute {
        if !compute {
            return Err(format!(
                "{name}: sandbox.provider: compute requires target.compute.kind: docker_sandbox"
            ));
        }
        crate::docker_sandbox::validate_policy(&project.sandbox, has_extends, &[])
            .map_err(|error| format!("{name}: {error}"))?;
    } else if compute {
        return Err(format!(
            "{name}: docker_sandbox only supports sandbox.provider: compute; inner native enforcement is not supported"
        ));
    }
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
    if let Some(target) = &mut project.target {
        target
            .capture()
            .map_err(|error| format!("{name}: {error}"))?;
    }
    let target = project.target.filter(|target| !target.is_local_host());
    Ok(Captured {
        source: Some(name),
        policy: project.sandbox,
        target,
        provider,
    })
}

pub fn from_environment(name: &str) -> Result<SandboxSettings, String> {
    let value = std::env::var(name)
        .map_err(|error| format!("cannot read sandbox settings environment '{name}': {error}"))?;
    serde_json::from_str(&value)
        .map_err(|error| format!("invalid sandbox settings environment '{name}': {error}"))
}
