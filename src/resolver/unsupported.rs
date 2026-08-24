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

    pub(crate) fn interrupt(&self) -> Result<bool, String> {
        Ok(false)
    }

    pub(crate) fn control_outcome(&self) -> Option<super::ResolverControlOutcome> {
        None
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
    _on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<(), String> {
    Err("DuckDB extension preparation is supported only on macOS".to_string())
}

pub(crate) fn resolve_python(
    requirements: &[String],
    _configuration: &super::ManagedPythonResolverConfiguration,
    _managed_r: Option<&ManagedR>,
    _on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    if requirements.is_empty() {
        Ok(ManagedPython {
            requirements: crate::worker_protocol::default_python_requirement_manifest(),
        })
    } else {
        Err("managed Python environments are supported only on macOS".to_string())
    }
}

pub(crate) fn resolve_python_manifest(
    requirements: crate::worker_protocol::PythonRequirementManifest,
    _configuration: &super::ManagedPythonResolverConfiguration,
    _managed_r: Option<&ManagedR>,
    _on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    crate::python_requirement::validate_all(&requirements.packages)?;
    crate::python_requirement::validate_version_constraints(&requirements.python_version)?;
    Err("managed Python environments are supported only on macOS".to_string())
}

pub(crate) fn resolve_python_host(
    requirements: crate::worker_protocol::PythonRequirementManifest,
    configuration: &super::ManagedPythonResolverConfiguration,
    managed_r: Option<&ManagedR>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    resolve_python_manifest(requirements, configuration, managed_r, on_started)
}

pub(crate) fn resolve_python_version(
    constraints: Vec<String>,
    _configuration: &super::ManagedPythonResolverConfiguration,
    _managed_r: Option<&ManagedR>,
    _on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<String, String> {
    crate::python_requirement::validate_version_constraints(&constraints)?;
    Err("managed Python versions are supported only on macOS".to_string())
}
