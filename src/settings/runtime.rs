//! Public runtime choices. Paths and declarations are captured after layering.

use serde::{Deserialize, Serialize, de::Error as _};
use serde_json::Value;
use std::path::PathBuf;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Resolution {
    #[default]
    Automatic,
    Explicit,
    StartupOnly,
    Disabled,
}

impl Resolution {
    pub(crate) fn automatic(self) -> bool {
        self == Self::Automatic
    }
    pub(crate) fn changes(self) -> bool {
        matches!(self, Self::Automatic | Self::Explicit)
    }
    pub(crate) fn enabled(self) -> bool {
        self != Self::Disabled
    }
    pub(crate) fn reject(self, language: &str) -> String {
        let policy = match self {
            Self::Automatic => "automatic",
            Self::Explicit => "explicit",
            Self::StartupOnly => "startup_only",
            Self::Disabled => "disabled",
        };
        format!(
            "{language} resolution is disabled by configuration for this operation ({policy}); use resolution: explicit for MCP requirements changes or automatic for runtime preparation"
        )
    }
}

#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct R {
    #[serde(
        deserialize_with = "super::sandbox::supplied",
        skip_serializing_if = "Option::is_none"
    )]
    pub executable: Option<PathBuf>,
    pub vanilla: bool,
    pub resolution: Resolution,
    #[serde(
        deserialize_with = "super::sandbox::supplied",
        skip_serializing_if = "Option::is_none"
    )]
    pub packages: Option<Vec<String>>,
}

pub(super) fn r<'de, D: serde::Deserializer<'de>>(deserializer: D) -> Result<Option<R>, D::Error> {
    let value = Value::deserialize(deserializer)?;
    if value.is_null() {
        return Ok(None);
    }
    if !value.is_object() {
        return Err(D::Error::custom(
            "expected an R mapping or path; use null to clear R settings",
        ));
    }
    serde_path_to_error::deserialize(value)
        .map(Some)
        .map_err(D::Error::custom)
}

fn managed<'de, D: serde::Deserializer<'de>>(deserializer: D) -> Result<ManagedPython, D::Error> {
    let value = Value::deserialize(deserializer)?;
    if !value.is_object() {
        return Err(D::Error::custom("expected managed options as a mapping"));
    }
    serde_path_to_error::deserialize(value).map_err(D::Error::custom)
}

impl R {
    pub(super) fn capture(&mut self) -> Result<(), String> {
        if let Some(path) = &mut self.executable {
            *path = capture_path(path, "r.executable")?;
        }
        if let Some(packages) = &self.packages {
            crate::worker_client::validate_r_requirements(packages)
                .map_err(|error| format!("r.packages: {error}"))?;
            if !self.resolution.enabled() && !packages.is_empty() {
                return Err("r.packages: nonempty packages require R resolution; use resolution: startup_only to prepare them once".into());
            }
        }
        Ok(())
    }
}

#[derive(Clone, Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct ManagedPython {
    #[serde(deserialize_with = "super::sandbox::supplied")]
    pub version: Option<String>,
    #[serde(deserialize_with = "super::sandbox::supplied")]
    pub packages: Option<Vec<String>>,
    pub resolution: Resolution,
}

impl ManagedPython {
    fn capture(&self) -> Result<(), String> {
        if let Some(version) = &self.version {
            crate::python_requirement::validate_version_constraint(version)
                .map_err(|error| format!("managed.version: {error}"))?;
        }
        if let Some(packages) = &self.packages {
            crate::python_requirement::validate_all(packages)
                .map_err(|error| format!("managed.packages: {error}"))?;
        }
        if !self.resolution.enabled() {
            return Err("managed.resolution: disabled is unsupported; choose an existing environment or startup_only".into());
        }
        Ok(())
    }
    pub(crate) fn manifest(
        &self,
        without_r: bool,
    ) -> crate::worker_protocol::PythonRequirementManifest {
        let mut manifest = if without_r {
            crate::worker_protocol::default_native_python_requirement_manifest()
        } else {
            crate::worker_protocol::default_python_requirement_manifest()
        };
        if let Some(packages) = &self.packages {
            manifest.packages = packages.clone();
        }
        if let Some(version) = &self.version {
            manifest.python_version = vec![version.clone()];
        }
        manifest.normalized()
    }
}

#[derive(Clone)]
pub(crate) enum Python {
    Existing(PathBuf),
    Managed(ManagedPython),
    FirstAvailable(Vec<Candidate>),
}

#[derive(Clone)]
pub(crate) enum Candidate {
    Existing(PathBuf),
    ActiveVenv(Option<PathBuf>),
    Managed(ManagedPython),
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum PythonMapping {
    Existing(PathBuf),
    Managed(#[serde(deserialize_with = "managed")] ManagedPython),
    FirstAvailable(Vec<Value>),
}

impl<'de> Deserialize<'de> for Python {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = Value::deserialize(deserializer)?;
        if let Some(path) = value.as_str() {
            return Ok(Self::Existing(path.into()));
        }
        if !value.is_object() || value.as_object().is_some_and(|mapping| mapping.len() != 1) {
            return Err(D::Error::custom(
                "expected a path or exactly one of managed, existing, first_available; clear python with null before changing variants",
            ));
        }
        match serde_path_to_error::deserialize(value).map_err(D::Error::custom)? {
            PythonMapping::Existing(path) => Ok(Self::Existing(path)),
            PythonMapping::Managed(options) => Ok(Self::Managed(options)),
            PythonMapping::FirstAvailable(values) => {
                if values.is_empty() {
                    return Err(D::Error::custom("first_available must not be empty"));
                }
                let mut active = false;
                let count = values.len();
                let mut candidates = Vec::new();
                for (index, value) in values.into_iter().enumerate() {
                    let candidate = if value == "active_venv" {
                        if active {
                            return Err(D::Error::custom(
                                "first_available permits only one active_venv",
                            ));
                        }
                        active = true;
                        Candidate::ActiveVenv(None)
                    } else {
                        match serde_json::from_value::<PythonMapping>(value)
                            .map_err(D::Error::custom)?
                        {
                            PythonMapping::Existing(path) => Candidate::Existing(path),
                            PythonMapping::Managed(options) if index + 1 == count => {
                                Candidate::Managed(options)
                            }
                            _ => {
                                return Err(D::Error::custom(
                                    "first_available permits existing paths, active_venv, and at most one managed candidate last; nested chains are unsupported",
                                ));
                            }
                        }
                    };
                    candidates.push(candidate);
                }
                Ok(Self::FirstAvailable(candidates))
            }
        }
    }
}

impl Python {
    pub(super) fn capture(&mut self) -> Result<(), String> {
        match self {
            Self::Existing(path) => *path = capture_path(path, "python.existing")?,
            Self::Managed(options) => options
                .capture()
                .map_err(|error| format!("python.{error}"))?,
            Self::FirstAvailable(candidates) => {
                for (index, candidate) in candidates.iter_mut().enumerate() {
                    let context = format!("python.first_available[{index}]");
                    match candidate {
                        Candidate::Existing(path) => *path = capture_path(path, &context)?,
                        Candidate::ActiveVenv(path) => {
                            *path = std::env::var_os("VIRTUAL_ENV")
                                .filter(|value| !value.is_empty())
                                .map(PathBuf::from)
                                .map(|path| capture_path(&path, &context))
                                .transpose()?;
                        }
                        Candidate::Managed(options) => options
                            .capture()
                            .map_err(|error| format!("{context}.{error}"))?,
                    }
                }
            }
        }
        Ok(())
    }
}

fn capture_path(path: &std::path::Path, context: &str) -> Result<PathBuf, String> {
    if path.as_os_str().is_empty() {
        return Err(format!("{context}: expected a nonempty path"));
    }
    let path = if let Ok(relative) = path.strip_prefix("~") {
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .filter(|home| home.is_absolute())
            .ok_or_else(|| format!("{context}: home expansion requires an absolute HOME"))?;
        home.join(relative)
    } else {
        path.to_path_buf()
    };
    std::path::absolute(path).map_err(|error| format!("{context}: cannot locate path: {error}"))
}
