use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::sync::Arc;

#[cfg(target_os = "macos")]
mod managed_duckdb;
#[cfg(target_os = "macos")]
mod managed_python;
#[cfg(target_os = "macos")]
mod managed_r;
#[cfg(target_os = "macos")]
mod process;
#[cfg(not(target_os = "macos"))]
mod unsupported;

#[derive(Clone)]
pub(crate) struct ManagedPythonResolverConfiguration {
    environment: Arc<BTreeMap<OsString, OsString>>,
    managed_r: Option<ManagedR>,
}

impl ManagedPythonResolverConfiguration {
    pub(crate) fn capture() -> Self {
        let environment = std::env::vars_os()
            .filter(|(name, _)| is_uv_environment_variable(name) && name != "UV_OFFLINE")
            .collect();
        Self {
            environment: Arc::new(environment),
            managed_r: None,
        }
    }

    pub(crate) fn with_managed_r(mut self, managed_r: ManagedR) -> Self {
        self.managed_r = Some(managed_r);
        self
    }

    #[cfg(target_os = "macos")]
    fn rscript(&self) -> Result<&std::path::Path, String> {
        self.managed_r
            .as_ref()
            .map(ManagedR::rscript)
            .ok_or_else(|| "managed Python resolver requires a managed R environment".to_string())
    }

    #[cfg(target_os = "macos")]
    fn configure(&self, command: &mut std::process::Command) -> Result<(), String> {
        self.managed_r
            .as_ref()
            .ok_or_else(|| "managed Python resolver requires a managed R environment".to_string())?
            .configure_resolver(command)?;
        for (name, _) in std::env::vars_os().filter(|(name, _)| is_uv_environment_variable(name)) {
            command.env_remove(name);
        }
        command
            .envs(self.environment.iter())
            .env_remove("UV_OFFLINE");
        Ok(())
    }
}

fn is_uv_environment_variable(name: &OsStr) -> bool {
    name.as_encoded_bytes().starts_with(b"UV_")
}

#[cfg(target_os = "macos")]
pub(crate) use managed_duckdb::resolve_duckdb_extensions;
#[cfg(target_os = "macos")]
pub(crate) use managed_python::{
    ManagedPython, resolve_python, resolve_python_host, resolve_python_manifest,
    resolve_python_version,
};
#[cfg(target_os = "macos")]
pub(crate) use managed_r::{ManagedR, resolve_r};
#[cfg(target_os = "macos")]
pub(crate) use process::ResolverStopHandle;
#[cfg(not(target_os = "macos"))]
pub(crate) use unsupported::{
    ManagedPython, ManagedR, ResolverStopHandle, resolve_duckdb_extensions, resolve_python,
    resolve_python_host, resolve_python_manifest, resolve_python_version, resolve_r,
};
