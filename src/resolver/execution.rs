//! Select the execution host without moving session transactions into the resolver.

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
            // The trusted remote configuration resolves uv in the operation
            // that first needs it, using the selected managed R environment.
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
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    match configuration {
        PythonConfiguration::Local { preparation, .. } => preparation.call(
            Operation::Python {
                requirements,
                r: managed_r.cloned(),
            },
            on_started,
        ),
        PythonConfiguration::Ssh(remote) => remote.call(
            Operation::Python {
                requirements,
                r: managed_r.cloned(),
            },
            on_started,
        ),
        #[cfg(not(unix))]
        PythonConfiguration::Direct(configuration) => {
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
        PythonConfiguration::Local { preparation, .. } => preparation.call(
            Operation::LocalPythonVersion {
                constraints,
                r: managed_r.cloned(),
            },
            on_started,
        ),
        PythonConfiguration::Ssh(remote) => remote.call(
            Operation::PythonVersion {
                constraints,
                r: managed_r
                    .ok_or_else(|| {
                        "remote Python version resolution requires managed R".to_string()
                    })?
                    .clone(),
            },
            on_started,
        ),
        #[cfg(not(unix))]
        PythonConfiguration::Direct(configuration) => {
            super::resolve_python_version(constraints, configuration, on_started)
        }
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
    let PythonConfiguration::Local { preparation, .. } = configuration else {
        return Err("Python-backed DuckDB preparation requires a local resolver".into());
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
