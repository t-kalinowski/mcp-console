use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::sync::Arc;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ResolverControlOutcome {
    Interrupted,
    Cancelled,
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
mod managed_duckdb;
#[cfg(any(target_os = "macos", target_os = "linux"))]
mod managed_python;
#[cfg(any(target_os = "macos", target_os = "linux"))]
mod managed_r;
#[cfg(any(target_os = "macos", target_os = "linux"))]
mod process;
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
mod unsupported;

#[derive(Clone)]
pub(crate) struct ManagedPythonResolverConfiguration {
    environment: Arc<BTreeMap<OsString, OsString>>,
    reticulate_uv: Option<OsString>,
}

impl ManagedPythonResolverConfiguration {
    pub(crate) fn capture() -> Self {
        let environment = std::env::vars_os()
            .filter(|(name, _)| is_uv_environment_variable(name) && name != "UV_OFFLINE")
            .collect();
        let reticulate_uv = std::env::var_os("RETICULATE_UV");
        Self {
            environment: Arc::new(environment),
            reticulate_uv,
        }
    }

    #[cfg(any(target_os = "macos", target_os = "linux"))]
    fn explicit_uv(&self) -> Option<&OsStr> {
        self.reticulate_uv.as_deref()
    }

    #[cfg(any(target_os = "macos", target_os = "linux"))]
    pub(crate) fn has_uv(&self) -> bool {
        self.reticulate_uv.is_some()
    }

    #[cfg(any(target_os = "macos", target_os = "linux"))]
    pub(crate) fn set_default_uv(&mut self, uv: impl Into<OsString>) {
        if self.reticulate_uv.is_none() {
            self.reticulate_uv = Some(uv.into());
        }
    }

    #[cfg(any(target_os = "macos", target_os = "linux"))]
    fn configure_uv(&self, command: &mut std::process::Command, uv: &OsStr) {
        for (name, _) in std::env::vars_os().filter(|(name, _)| is_uv_environment_variable(name)) {
            command.env_remove(name);
        }
        command
            .envs(self.environment.iter())
            .env("RETICULATE_UV", uv)
            .env_remove("UV_OFFLINE");
    }

    #[cfg(any(target_os = "macos", target_os = "linux"))]
    fn configure_uv_bootstrap(&self, command: &mut std::process::Command) {
        self.configure_uv(command, OsStr::new("managed"));
    }

    #[cfg(any(target_os = "macos", target_os = "linux"))]
    fn configure(
        &self,
        managed_r: &ManagedR,
        command: &mut std::process::Command,
    ) -> Result<(), String> {
        managed_r.configure_resolver(command)?;
        let uv = self
            .reticulate_uv
            .as_deref()
            .ok_or_else(|| "managed Python resolver has no `uv` executable".to_string())?;
        self.configure_uv(command, uv);
        Ok(())
    }
}

fn is_uv_environment_variable(name: &OsStr) -> bool {
    name.as_encoded_bytes().starts_with(b"UV_")
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
pub(crate) use managed_duckdb::resolve_duckdb_extensions;
#[cfg(any(target_os = "macos", target_os = "linux"))]
pub(crate) use managed_python::{
    ManagedPython, resolve_python, resolve_python_host, resolve_python_manifest,
    resolve_python_version,
};
#[cfg(any(target_os = "macos", target_os = "linux"))]
pub(crate) use managed_r::{
    ManagedR, ManagedRResolverConfiguration, discover_r_resolver, resolve_r, resolve_r_with,
};
#[cfg(any(target_os = "macos", target_os = "linux"))]
pub(crate) use process::ResolverStopHandle;
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub(crate) use unsupported::{
    ManagedPython, ManagedR, ManagedRResolverConfiguration, ResolverStopHandle,
    resolve_duckdb_extensions, resolve_python, resolve_python_host, resolve_python_manifest,
    resolve_python_version, resolve_r, resolve_r_with,
};
