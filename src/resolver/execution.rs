//! Select the execution host without moving session transactions into the resolver.
//!
//! Callers trust the preparation command for explicit and automatic resolution.
//! Enforcement of that boundary belongs there; see docs/REQUIREMENTS.md.

use super::preparation::{Operation, Preparation};
use super::{ManagedPython, ManagedR, ResolverStopHandle};
use crate::worker_protocol::PythonRequirementManifest;

#[derive(Clone)]
pub(crate) enum Bootstrap {
    Local(Preparation),
    Ssh(Preparation),
}

#[derive(Clone)]
pub(crate) enum RConfiguration {
    Local(Preparation),
    Ssh(Preparation),
}

#[derive(Clone)]
pub(crate) enum PythonConfiguration {
    Local {
        preparation: Preparation,
        has_uv: bool,
    },
    Ssh(Preparation),
    #[cfg(not(unix))]
    Direct(super::ManagedPythonResolverConfiguration),
}

impl Bootstrap {
    pub(crate) fn prepare(
        &self,
        python: &mut PythonConfiguration,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<RConfiguration, String> {
        match (self, python) {
            (Self::Local(preparation), PythonConfiguration::Local { .. }) => {
                preparation.call::<()>(Operation::Bootstrap, on_started)?;
                Ok(RConfiguration::Local(preparation.clone()))
            }
            (Self::Ssh(remote), PythonConfiguration::Ssh(_)) => {
                remote.call::<()>(Operation::Bootstrap, on_started)?;
                Ok(RConfiguration::Ssh(remote.clone()))
            }
            _ => unreachable!("resolver configurations belong to one execution host"),
        }
    }
}

impl PythonConfiguration {
    pub(crate) fn has_uv(&self) -> bool {
        match self {
            Self::Local { has_uv, .. } => *has_uv,
            #[cfg(not(unix))]
            Self::Direct(configuration) => configuration.has_uv(),
            // Discovery requires remote uv for sans-R managed sessions.
            // R-present sessions can prepare it when their first operation needs it.
            Self::Ssh(_) => true,
        }
    }
    pub(crate) fn has_direct_local_uv(&self) -> bool {
        match self {
            Self::Local { has_uv, .. } => *has_uv,
            #[cfg(not(unix))]
            Self::Direct(configuration) => configuration.has_uv(),
            Self::Ssh(_) => false,
        }
    }
    pub(crate) fn set_resolved_uv(&mut self, _uv: std::ffi::OsString) {
        match self {
            Self::Local { has_uv, .. } => *has_uv = true,
            #[cfg(not(unix))]
            Self::Direct(configuration) => configuration.set_resolved_uv(_uv),
            Self::Ssh(_) => unreachable!("remote uv stays remote"),
        }
    }
}

impl RConfiguration {
    pub(crate) fn resolve_uv(
        &self,
        managed_r: &ManagedR,
        python: &PythonConfiguration,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<std::ffi::OsString, String> {
        let (Self::Local(preparation), PythonConfiguration::Local { .. }) = (self, python) else {
            unreachable!("remote uv stays remote")
        };
        preparation.call(
            Operation::Uv {
                r: managed_r.clone(),
            },
            on_started,
        )
    }

    pub(crate) fn resolve_r(
        &self,
        requirements: Vec<String>,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<ManagedR, String> {
        match self {
            Self::Local(preparation) => preparation.call(Operation::R { requirements }, on_started),
            Self::Ssh(remote) => remote.call(Operation::R { requirements }, on_started),
        }
    }
}

pub(crate) fn resolve_python_manifest(
    requirements: PythonRequirementManifest,
    configuration: &PythonConfiguration,
    managed_r: Option<&ManagedR>,
    selected_python: Option<&std::path::Path>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    match configuration {
        PythonConfiguration::Local { preparation, .. } | PythonConfiguration::Ssh(preparation) => {
            preparation.call(
                Operation::Python {
                    requirements,
                    r: managed_r.cloned(),
                    selected_python: selected_python.map(std::path::Path::to_path_buf),
                },
                on_started,
            )
        }
        #[cfg(not(unix))]
        PythonConfiguration::Direct(configuration) => {
            if selected_python.is_some() {
                return Err(
                    "live managed Python preparation is unsupported on this platform".into(),
                );
            }
            super::resolve_python_manifest(requirements, configuration, on_started)
        }
    }
}

pub(crate) fn resolve_python_version(
    constraints: Vec<String>,
    configuration: &PythonConfiguration,
    managed_r: Option<&ManagedR>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<String, String> {
    match configuration {
        PythonConfiguration::Local { preparation, .. } | PythonConfiguration::Ssh(preparation) => {
            preparation.call(
                Operation::PythonVersion {
                    constraints,
                    r: managed_r.cloned(),
                },
                on_started,
            )
        }
        #[cfg(not(unix))]
        PythonConfiguration::Direct(configuration) => {
            super::resolve_python_version(constraints, configuration, on_started)
        }
    }
}

pub(crate) fn inspect_native(
    configuration: &PythonConfiguration,
    executable: &std::path::Path,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<crate::python::NativePython, String> {
    match configuration {
        PythonConfiguration::Local { preparation, .. } | PythonConfiguration::Ssh(preparation) => {
            preparation.call(
                Operation::InspectPython {
                    executable: executable.to_path_buf(),
                },
                on_started,
            )
        }
        #[cfg(not(unix))]
        PythonConfiguration::Direct(_) => crate::python::inspect_native(executable, on_started),
    }
}

pub(crate) fn resolve_duckdb_extensions(
    preparation: Option<&Preparation>,
    managed_r: &ManagedR,
    extensions: &[String],
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<(), String> {
    let preparation = preparation.ok_or("DuckDB resolver has no preparation owner")?;
    preparation.call(
        Operation::Duckdb {
            r: managed_r.clone(),
            extensions: extensions.to_vec(),
        },
        on_started,
    )
}

pub(crate) fn resolve_python_duckdb_extensions(
    configuration: &PythonConfiguration,
    python: &ManagedPython,
    extensions: &[String],
    extension_directory: &std::path::Path,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<(), String> {
    let preparation = match configuration {
        PythonConfiguration::Local { preparation, .. } | PythonConfiguration::Ssh(preparation) => {
            preparation
        }
        #[cfg(not(unix))]
        PythonConfiguration::Direct(_) => {
            return Err("Python-backed DuckDB preparation requires a Unix resolver".into());
        }
    };
    preparation.call(
        Operation::DuckdbPython {
            python: python.clone(),
            extensions: extensions.to_vec(),
            extension_directory: extension_directory.to_path_buf(),
        },
        on_started,
    )
}
