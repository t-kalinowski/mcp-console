//! Broker calls without session transactions or execution-host differences.

use super::preparation::{Operation, Preparation};
use super::{ManagedPython, ManagedR, ResolverStopHandle};
use crate::worker_protocol::PythonRequirementManifest;

#[derive(Clone)]
pub(crate) struct Bootstrap(pub(crate) Preparation);

#[derive(Clone)]
pub(crate) struct RConfiguration(Preparation);

#[derive(Clone)]
pub(crate) struct PythonConfiguration {
    pub(crate) preparation: Preparation,
    pub(crate) direct_uv: bool,
}

impl Bootstrap {
    pub(crate) fn prepare(
        &self,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<RConfiguration, String> {
        self.0.call::<()>(Operation::Bootstrap, on_started)?;
        Ok(RConfiguration(self.0.clone()))
    }
}

impl RConfiguration {
    pub(crate) fn resolve_r(
        &self,
        requirements: Vec<String>,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<ManagedR, String> {
        self.0.call(Operation::R { requirements }, on_started)
    }
}

pub(crate) fn resolve_python_manifest(
    requirements: PythonRequirementManifest,
    configuration: &PythonConfiguration,
    managed_r: Option<&ManagedR>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    configuration.preparation.call(
        Operation::Python {
            requirements,
            r: managed_r.cloned(),
        },
        on_started,
    )
}

pub(crate) fn resolve_python_version(
    constraints: Vec<String>,
    configuration: &PythonConfiguration,
    managed_r: Option<&ManagedR>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<String, String> {
    configuration.preparation.call(
        Operation::PythonVersion {
            constraints,
            r: managed_r
                .ok_or("Python version resolution requires managed R")?
                .clone(),
        },
        on_started,
    )
}

pub(crate) fn resolve_duckdb_extensions(
    preparation: &Preparation,
    managed_r: &ManagedR,
    extensions: &[String],
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<(), String> {
    preparation.call(
        Operation::Duckdb {
            r: managed_r.clone(),
            extensions: extensions.to_vec(),
        },
        on_started,
    )
}
