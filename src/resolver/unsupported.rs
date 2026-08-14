use std::collections::BTreeMap;

#[derive(Clone)]
pub(crate) struct ManagedPython {
    requirements: crate::worker_protocol::PythonRequirementManifest,
}

#[derive(Clone)]
pub(crate) struct ManagedR;

#[derive(Clone)]
pub(crate) struct ResolverStopHandle;

impl ResolverStopHandle {
    pub(crate) fn stop(&self) -> Result<(), String> {
        Ok(())
    }
}

impl ManagedPython {
    pub(crate) fn requirements(&self) -> &crate::worker_protocol::PythonRequirementManifest {
        &self.requirements
    }

    pub(crate) fn with_retained_requirements(
        mut self,
        requirements: crate::worker_protocol::PythonRequirementManifest,
    ) -> Self {
        self.requirements = requirements;
        self
    }
}

impl ManagedR {
    pub(crate) fn requirements(&self) -> &[String] {
        &[]
    }

    pub(crate) fn library(&self) -> &std::path::Path {
        unreachable!("managed R libraries are supported only on macOS")
    }
}

pub(crate) fn resolve_r(
    _requirements: Vec<String>,
    _on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedR, String> {
    Err("managed R libraries are supported only on macOS".to_string())
}

pub(crate) fn resolve_duckdb_extensions(
    _managed_r: &ManagedR,
    _extensions: &[String],
    _extension_directory: &std::path::Path,
    _on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<(), String> {
    Err("DuckDB extension preparation is supported only on macOS".to_string())
}

pub(crate) fn resolve_python(
    requirements: &[String],
    _on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<Option<ManagedPython>, String> {
    if requirements.is_empty() {
        Ok(None)
    } else {
        Err("managed Python environments are supported only on macOS".to_string())
    }
}

pub(crate) fn resolve_python_manifest(
    _requirements: crate::worker_protocol::PythonRequirementManifest,
    _environment: BTreeMap<String, String>,
    _on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    Err("managed Python environments are supported only on macOS".to_string())
}

pub(crate) fn resolve_python_host(
    requirements: crate::worker_protocol::PythonRequirementManifest,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    resolve_python_manifest(requirements, BTreeMap::new(), on_started)
}

pub(crate) fn resolve_python_version(
    _constraints: Vec<String>,
    _environment: BTreeMap<String, String>,
    _on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<String, String> {
    Err("managed Python versions are supported only on macOS".to_string())
}
