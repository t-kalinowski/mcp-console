//! Select the execution host without moving session transactions into the resolver.

use super::{ManagedPython, ManagedR, ResolverStopHandle};
use crate::ssh::preparation::{Operation, Preparation};
use crate::worker_protocol::PythonRequirementManifest;

#[derive(Clone)]
pub(crate) enum Bootstrap {
    Local(super::ManagedRBootstrap),
    Ssh(Preparation),
}

#[derive(Clone)]
pub(crate) enum RConfiguration {
    Local(super::ManagedRResolverConfiguration),
    Ssh(Preparation),
}

#[derive(Clone)]
pub(crate) enum PythonConfiguration {
    Local(super::ManagedPythonResolverConfiguration),
    Ssh(Preparation),
}

impl Bootstrap {
    pub(crate) fn prepare(
        &self,
        python: &mut PythonConfiguration,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<RConfiguration, String> {
        match (self, python) {
            (Self::Local(bootstrap), PythonConfiguration::Local(python)) => bootstrap
                .prepare(python, on_started)
                .map(RConfiguration::Local),
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
            Self::Local(configuration) => configuration.has_uv(),
            // The trusted remote configuration resolves uv in the operation
            // that first needs it, using the selected managed R environment.
            Self::Ssh(_) => true,
        }
    }
    pub(crate) fn set_resolved_uv(&mut self, uv: std::ffi::OsString) {
        let Self::Local(configuration) = self else {
            unreachable!("remote uv stays remote")
        };
        configuration.set_resolved_uv(uv);
    }
}

impl RConfiguration {
    pub(crate) fn resolve_uv(
        &self,
        managed_r: &ManagedR,
        python: &PythonConfiguration,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<std::ffi::OsString, String> {
        let (Self::Local(configuration), PythonConfiguration::Local(python)) = (self, python)
        else {
            unreachable!("remote uv stays remote")
        };
        configuration.resolve_uv(managed_r, python, on_started)
    }

    pub(crate) fn resolve_r(
        &self,
        requirements: Vec<String>,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<ManagedR, String> {
        match self {
            Self::Local(configuration) => {
                super::resolve_r_with(configuration, requirements, on_started)
            }
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
        PythonConfiguration::Local(configuration) => {
            super::resolve_python_manifest(requirements, configuration, managed_r, on_started)
        }
        PythonConfiguration::Ssh(remote) => remote.call(
            Operation::Python {
                requirements,
                r: managed_r.cloned(),
            },
            on_started,
        ),
    }
}

pub(crate) fn resolve_python_version(
    constraints: Vec<String>,
    configuration: &PythonConfiguration,
    managed_r: &ManagedR,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<String, String> {
    match configuration {
        PythonConfiguration::Local(configuration) => {
            super::resolve_python_version(constraints, configuration, managed_r, on_started)
        }
        PythonConfiguration::Ssh(remote) => remote.call(
            Operation::PythonVersion {
                constraints,
                r: managed_r.clone(),
            },
            on_started,
        ),
    }
}

pub(crate) fn resolve_duckdb_extensions(
    remote: Option<&Preparation>,
    managed_r: &ManagedR,
    extensions: &[String],
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<(), String> {
    match remote {
        None => super::resolve_duckdb_extensions(managed_r, extensions, on_started),
        Some(remote) => remote.call(
            Operation::Duckdb {
                r: managed_r.clone(),
                extensions: extensions.to_vec(),
            },
            on_started,
        ),
    }
}
