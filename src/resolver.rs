use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::sync::Arc;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ResolverControlOutcome {
    Interrupted,
    Cancelled,
}

#[cfg(target_os = "macos")]
mod managed_duckdb;
#[cfg(target_os = "macos")]
mod managed_python;
#[cfg(target_os = "macos")]
mod managed_r;
#[cfg(target_os = "macos")]
mod process;
#[cfg(target_os = "macos")]
mod python_version;
#[cfg(not(target_os = "macos"))]
mod unsupported;

#[derive(Clone)]
pub(crate) struct ManagedPythonResolverConfiguration {
    environment: Arc<BTreeMap<OsString, OsString>>,
    reticulate_uv: Option<OsString>,
    uv: Option<OsString>,
}

impl ManagedPythonResolverConfiguration {
    pub(crate) fn capture() -> Self {
        let environment = std::env::vars_os()
            .filter(|(name, _)| is_uv_environment_variable(name) && name != "UV_OFFLINE")
            .collect();
        let reticulate_uv = std::env::var_os("RETICULATE_UV");
        let uv = reticulate_uv
            .as_ref()
            .filter(|uv| uv.as_os_str() != OsStr::new("managed"))
            .cloned();
        Self {
            environment: Arc::new(environment),
            reticulate_uv,
            uv,
        }
    }

    #[cfg(target_os = "macos")]
    fn explicit_uv(&self) -> Option<&OsStr> {
        self.reticulate_uv.as_deref()
    }

    #[cfg(target_os = "macos")]
    fn uv(&self) -> Result<&OsStr, String> {
        self.uv
            .as_deref()
            .ok_or_else(|| "managed Python resolver has no `uv` executable".to_string())
    }

    #[cfg(target_os = "macos")]
    fn reticulate_uv(&self) -> Result<&OsStr, String> {
        self.reticulate_uv
            .as_deref()
            .or(self.uv.as_deref())
            .ok_or_else(|| "managed Python resolver has no reticulate `uv` selection".to_string())
    }

    #[cfg(target_os = "macos")]
    fn python_preference(&self) -> Option<&OsStr> {
        self.environment.iter().find_map(|(name, value)| {
            (name.as_os_str() == OsStr::new("UV_PYTHON_PREFERENCE")).then_some(value.as_os_str())
        })
    }

    #[cfg(target_os = "macos")]
    pub(crate) fn has_uv(&self) -> bool {
        self.uv.is_some()
    }

    #[cfg(target_os = "macos")]
    pub(crate) fn set_default_uv(&mut self, uv: impl Into<OsString>) {
        let uv = uv.into();
        if self.uv.is_none() {
            self.uv = Some(uv.clone());
        }
        if self.reticulate_uv.is_none() {
            self.reticulate_uv = Some(uv);
        }
    }

    #[cfg(target_os = "macos")]
    fn configure_uv(&self, command: &mut std::process::Command, uv: &OsStr) {
        for (name, _) in std::env::vars_os().filter(|(name, _)| is_uv_environment_variable(name)) {
            command.env_remove(name);
        }
        command
            .envs(self.environment.iter())
            .env("RETICULATE_UV", uv)
            .env_remove("UV_OFFLINE");
    }

    #[cfg(target_os = "macos")]
    fn configure_uv_bootstrap(&self, command: &mut std::process::Command) {
        self.configure_uv(command, OsStr::new("managed"));
    }

    #[cfg(target_os = "macos")]
    fn configure(
        &self,
        managed_r: &ManagedR,
        command: &mut std::process::Command,
    ) -> Result<(), String> {
        managed_r.configure_resolver(command)?;
        let uv = self.reticulate_uv()?;
        self.configure_uv(command, uv);
        Ok(())
    }

    #[cfg(target_os = "macos")]
    fn configure_direct(
        &self,
        managed_r: Option<&ManagedR>,
        command: &mut std::process::Command,
    ) -> Result<(), String> {
        if let Some(managed_r) = managed_r {
            managed_r.configure_resolver(command)?;
        }
        let uv = self.reticulate_uv()?;
        self.configure_uv(command, uv);
        Ok(())
    }
}

fn is_uv_environment_variable(name: &OsStr) -> bool {
    name.as_encoded_bytes().starts_with(b"UV_")
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;

    fn configuration(reticulate_uv: Option<&str>, uv: Option<&str>) -> ManagedPythonResolverConfiguration {
        ManagedPythonResolverConfiguration {
            environment: Arc::new(BTreeMap::new()),
            reticulate_uv: reticulate_uv.map(OsString::from),
            uv: uv.map(OsString::from),
        }
    }

    #[test]
    fn preserves_managed_reticulate_selector_with_resolved_executable() {
        let mut configuration = configuration(Some("managed"), None);
        assert!(!configuration.has_uv());

        configuration.set_default_uv("/managed/uv");

        assert!(configuration.has_uv());
        assert_eq!(configuration.uv().unwrap(), OsStr::new("/managed/uv"));
        assert_eq!(configuration.reticulate_uv().unwrap(), OsStr::new("managed"));
    }

    #[test]
    fn uses_default_executable_as_reticulate_selection() {
        let mut configuration = configuration(None, None);
        configuration.set_default_uv("/path/uv");

        assert_eq!(configuration.uv().unwrap(), OsStr::new("/path/uv"));
        assert_eq!(configuration.reticulate_uv().unwrap(), OsStr::new("/path/uv"));
    }
}

#[cfg(target_os = "macos")]
pub(crate) use managed_duckdb::resolve_duckdb_extensions;
#[cfg(target_os = "macos")]
pub(crate) use managed_python::{
    ManagedPython, resolve_python, resolve_python_host, resolve_python_manifest,
    resolve_python_version,
};
#[cfg(target_os = "macos")]
pub(crate) use managed_r::{
    ManagedR, ManagedRResolverConfiguration, discover_r_resolver, resolve_r, resolve_r_with,
};
#[cfg(target_os = "macos")]
pub(crate) use process::ResolverStopHandle;
#[cfg(not(target_os = "macos"))]
pub(crate) use unsupported::{
    ManagedPython, ManagedR, ManagedRResolverConfiguration, ResolverStopHandle,
    resolve_duckdb_extensions, resolve_python, resolve_python_host, resolve_python_manifest,
    resolve_python_version, resolve_r, resolve_r_with,
};
