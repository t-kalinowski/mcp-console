//! Trusted application settings, captured before starting a session or workload.

use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

mod yaml;

pub const ENVIRONMENT: &str = "MCP_CONSOLE_SANDBOX_SETTINGS";

/// Captured native policy and Console's additional writable paths.
#[derive(Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SandboxSettings {
    pub writable_roots: Vec<PathBuf>,
    pub policy: Map<String, Value>,
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Project {
    sandbox: Map<String, Value>,
}

pub fn discover() -> Result<(Option<&'static str>, SandboxSettings), String> {
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
            return Ok((None, SandboxSettings::default()));
        }
        Err(error) => return Err(format!("cannot inspect '{name}': {error}")),
        Ok(_) => {}
    }
    let source =
        std::fs::read_to_string(name).map_err(|error| format!("cannot read '{name}': {error}"))?;
    let value = yaml::load(&source).map_err(|error| format!("{name}: {error}"))?;
    let project: Project =
        serde_path_to_error::deserialize(value).map_err(|error| format!("{name}: {error}"))?;
    // These fields belong to Console's launch protocol and worker lifetime.
    // All other sandbox fields and values are interpreted by the native runner.
    for field in ["version", "lifecycle"] {
        if project.sandbox.contains_key(field) {
            return Err(format!("{name}: sandbox.{field} is managed by Console"));
        }
    }
    Ok((
        Some(name),
        SandboxSettings {
            policy: project.sandbox,
            ..Default::default()
        },
    ))
}

pub fn from_environment(name: &str) -> Result<SandboxSettings, String> {
    let value = std::env::var(name)
        .map_err(|error| format!("cannot read sandbox settings environment '{name}': {error}"))?;
    serde_json::from_str(&value)
        .map_err(|error| format!("invalid sandbox settings environment '{name}': {error}"))
}
