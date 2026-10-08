//! Trusted application settings, captured before starting a session or workload.

use std::ffi::OsStr;
use std::path::{Path, PathBuf};

use serde::Deserialize;
use serde_json::{Map, Value};

mod python;
mod sandbox;
pub(crate) use python::PythonChoice;
pub(crate) mod startup;

pub const ENVIRONMENT: &str = "MCP_CONSOLE_SANDBOX_SETTINGS";

/// Apply workload environment controls to a child, never the supervisor.
pub fn configure_environment(command: &mut std::process::Command, settings: &SandboxSettings) {
    if settings.get("inherit_environment") == Some(&Value::Bool(false)) {
        command.env_clear();
    }
    if let Some(Value::Object(environment)) = settings.get("environment") {
        command.envs(
            environment
                .iter()
                .map(|(name, value)| (name, value.as_str().expect("captured string environment"))),
        );
    }
}

/// Native policy values; application additions materialize on the execution host.
pub type SandboxSettings = Map<String, Value>;

#[derive(Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Cache {
    Console,
    Host,
}

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
    startup: Option<startup::Startup>,
    cache: Option<Cache>,
    python: Option<python::Python>,
    #[serde(deserialize_with = "sandbox::supplied_mapping")]
    r: Option<R>,
    languages: Option<Vec<crate::cell::Language>>,
    #[serde(default = "inherit_by_default")]
    inherit_environment: bool,
    #[serde(deserialize_with = "environment")]
    environment: std::collections::BTreeMap<String, String>,
    #[serde(deserialize_with = "sandbox::supplied_mapping")]
    sandbox: Option<sandbox::Sandbox>,
    #[serde(deserialize_with = "sandbox::mapping")]
    resolver: Resolver,
}

#[derive(Clone, Copy, Default, serde::Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct R {
    pub vanilla: bool,
}

fn inherit_by_default() -> bool {
    true
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Resolver {
    #[serde(deserialize_with = "sandbox::supplied")]
    inherit_environment: Option<bool>,
    #[serde(deserialize_with = "environment")]
    environment: std::collections::BTreeMap<String, String>,
    #[serde(deserialize_with = "sandbox::supplied_mapping")]
    sandbox: Option<sandbox::Sandbox>,
}

fn environment<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<std::collections::BTreeMap<String, String>, D::Error> {
    use serde::de::Error as _;
    let values: std::collections::BTreeMap<String, Value> = sandbox::mapping(deserializer)?;
    values
        .into_iter()
        .map(|(name, value)| {
            let value = value.as_str().ok_or_else(|| {
                D::Error::custom(format!("{name}: environment values must be strings"))
            })?;
            Ok((name, value.to_owned()))
        })
        .collect()
}

#[derive(Default)]
pub(crate) struct Captured {
    pub startup: Option<startup::Startup>,
    pub cache: Option<Cache>,
    pub python: Option<PythonChoice>,
    pub r: Option<R>,
    pub languages: Option<crate::cell::Languages>,
    pub source: Option<String>,
    pub policy: SandboxSettings,
    pub resolver: SandboxSettings,
    pub sandbox_requested: bool,
    pub resolver_sandbox_requested: bool,
}

pub fn discover(overrides: &[String], no_project_config: bool) -> Result<Captured, String> {
    let project = Path::new(".agents/console/config.yaml");
    let use_project = if no_project_config {
        false
    } else {
        match std::fs::symlink_metadata(project) {
            Ok(_) => true,
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
                ) =>
            {
                false
            }
            Err(error) => return Err(format!("cannot inspect '{}': {error}", project.display())),
        }
    };
    let path = if use_project {
        Some(PathBuf::from(project))
    } else {
        crate::console_paths::home_console_directory()?
            .map(|directory| directory.join("config.yaml"))
    };
    let value = crate::config::load(path.as_deref(), overrides)?;
    let configured = value.is_some();
    let name = if overrides.is_empty() && configured {
        path.expect("configuration came from a file")
            .to_string_lossy()
            .into_owned()
    } else {
        "configuration with CLI overrides".into()
    };
    let project: Project = serde_path_to_error::deserialize(
        value.unwrap_or_else(|| serde_json::json!({})),
    )
    .map_err(|error| format!("{name}: {error}; see docs/CONFIGURATION.md for the public format"))?;
    if let Some(startup) = &project.startup {
        startup
            .validate()
            .map_err(|error| format!("{name}: {error}"))?;
    }
    let languages = project
        .languages
        .map(|selected| {
            if selected.is_empty() {
                return Err(format!(
                    "{name}: languages must contain at least one of r, python, or sql"
                ));
            }
            let mut languages = crate::cell::Languages::default();
            for language in selected {
                match language {
                    crate::cell::Language::R => languages.r = true,
                    crate::cell::Language::Python => languages.python = true,
                    crate::cell::Language::Sql => languages.sql = true,
                }
            }
            Ok(languages)
        })
        .transpose()?;
    let sandbox_requested = project.sandbox.is_some();
    let resolver_sandbox_requested = project.resolver.sandbox.is_some();
    if cfg!(windows) && resolver_sandbox_requested {
        return Err(format!(
            "{name}: resolver.sandbox: explicit permissions are unsupported because Windows preparation runs with host permissions"
        ));
    }
    let mut policy = project
        .sandbox
        .unwrap_or_default()
        .compile(false)
        .map_err(|error| format!("{name}: {error}"))?;
    let mut resolver = project
        .resolver
        .sandbox
        .unwrap_or_default()
        .compile(true)
        .map_err(|error| format!("{name}: {error}"))?;
    let mut resolver_environment = project.environment.clone();
    resolver_environment.extend(project.resolver.environment);
    for (settings, inherit, environment) in [
        (
            &mut policy,
            project.inherit_environment,
            project.environment,
        ),
        (
            &mut resolver,
            project
                .resolver
                .inherit_environment
                .unwrap_or(project.inherit_environment),
            resolver_environment,
        ),
    ] {
        if !inherit {
            settings.insert("inherit_environment".into(), false.into());
        }
        if !environment.is_empty() {
            settings.insert(
                "environment".into(),
                serde_json::to_value(environment).expect("string environment"),
            );
        }
    }
    Ok(Captured {
        r: project.r,
        startup: project.startup,
        cache: project.cache,
        languages,
        python: project.python.map(python::Python::capture).transpose()?,
        source: configured.then_some(name),
        policy,
        resolver,
        sandbox_requested,
        resolver_sandbox_requested,
    })
}

pub fn from_environment(name: &str) -> Result<SandboxSettings, String> {
    let value = std::env::var(name)
        .map_err(|error| format!("cannot read sandbox settings environment '{name}': {error}"))?;
    serde_json::from_str(&value)
        .map_err(|error| format!("invalid sandbox settings environment '{name}': {error}"))
}
