use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::sync::Arc;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ResolverControlOutcome {
    Interrupted,
    Cancelled,
}

#[cfg(unix)]
mod managed_duckdb;
#[cfg(unix)]
mod managed_python;
#[cfg(unix)]
mod managed_r;
#[cfg(unix)]
mod process;
#[cfg(unix)]
mod python_version;
#[cfg(not(unix))]
mod unsupported;

#[derive(Clone)]
pub(crate) struct ManagedPythonResolverConfiguration {
    environment: Arc<BTreeMap<OsString, OsString>>,
    reticulate_uv: Option<OsString>,
    uv: Option<OsString>,
}

impl ManagedPythonResolverConfiguration {
    pub(crate) fn capture() -> Self {
        let mut environment = std::env::vars_os()
            .filter(|(name, _)| is_uv_environment_variable(name) && name != "UV_OFFLINE")
            .collect::<BTreeMap<_, _>>();
        normalize_python_preference(&mut environment);
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

    fn explicit_uv(&self) -> Option<&OsStr> {
        self.reticulate_uv.as_deref()
    }

    fn uv(&self) -> Result<&OsStr, String> {
        self.uv
            .as_deref()
            .ok_or_else(|| "managed Python resolver has no `uv` executable".to_string())
    }

    fn reticulate_uv(&self) -> Result<&OsStr, String> {
        self.reticulate_uv
            .as_deref()
            .or(self.uv.as_deref())
            .ok_or_else(|| "managed Python resolver has no reticulate `uv` selection".to_string())
    }

    fn python_preference(&self) -> Option<&OsStr> {
        self.environment.iter().find_map(|(name, value)| {
            (name.as_os_str() == OsStr::new("UV_PYTHON_PREFERENCE")).then_some(value.as_os_str())
        })
    }

    pub(crate) fn has_uv(&self) -> bool {
        self.uv.is_some()
    }

    pub(crate) fn set_default_uv(&mut self, uv: impl Into<OsString>) {
        let uv = uv.into();
        if self.reticulate_uv.is_none() {
            self.reticulate_uv = Some(uv.clone());
            self.uv = Some(uv);
        }
    }

    pub(crate) fn set_resolved_uv(&mut self, uv: impl Into<OsString>) {
        let uv = uv.into();
        if self.uv.is_none() {
            self.uv = Some(uv.clone());
        }
        if self.reticulate_uv.is_none() {
            self.reticulate_uv = Some(uv);
        }
    }

    fn configure_uv(&self, command: &mut std::process::Command, uv: &OsStr) {
        for (name, _) in std::env::vars_os().filter(|(name, _)| is_uv_environment_variable(name)) {
            command.env_remove(name);
        }
        command
            .envs(self.environment.iter())
            .env("RETICULATE_UV", uv)
            .env_remove("UV_OFFLINE");
    }

    fn configure_uv_bootstrap(&self, command: &mut std::process::Command) {
        self.configure_uv(command, OsStr::new("managed"));
    }

    fn configure_direct(&self, command: &mut std::process::Command) -> Result<(), String> {
        let uv = self.reticulate_uv()?;
        self.configure_uv(command, uv);
        if uv == OsStr::new("managed") {
            let executable = std::path::Path::new(self.uv()?);
            let root = executable
                .parent()
                .and_then(std::path::Path::parent)
                .ok_or_else(|| {
                    format!(
                        "reticulate managed `uv` executable has no cache root: `{}`",
                        executable.display()
                    )
                })?;
            command
                .env("UV_CACHE_DIR", root.join("cache"))
                .env("UV_PYTHON_INSTALL_DIR", root.join("python"));
        }
        Ok(())
    }
}

fn normalize_python_preference(environment: &mut BTreeMap<OsString, OsString>) {
    let managed_name = OsStr::new("UV_MANAGED_PYTHON");
    let system_name = OsStr::new("UV_NO_MANAGED_PYTHON");
    let managed = uv_flag_value(environment, managed_name);
    let system = uv_flag_value(environment, system_name);
    if managed == Some(false) {
        environment.remove(managed_name);
    }
    if system == Some(false) {
        environment.remove(system_name);
    }
    if environment.contains_key(OsStr::new("UV_PYTHON_PREFERENCE")) {
        return;
    }
    let (name, preference) = if managed == Some(true) && !environment.contains_key(system_name) {
        (managed_name, "only-managed")
    } else if system == Some(true) && !environment.contains_key(managed_name) {
        (system_name, "only-system")
    } else {
        return;
    };
    environment.remove(name);
    environment.insert(
        OsString::from("UV_PYTHON_PREFERENCE"),
        OsString::from(preference),
    );
}

fn uv_flag_value(environment: &BTreeMap<OsString, OsString>, name: &OsStr) -> Option<bool> {
    environment
        .get(name)
        .and_then(|value| value.to_str())
        .and_then(|value| match value.to_ascii_lowercase().as_str() {
            "1" | "true" | "t" | "yes" | "y" | "on" => Some(true),
            "0" | "false" | "f" | "no" | "n" | "off" => Some(false),
            _ => None,
        })
}

fn is_uv_environment_variable(name: &OsStr) -> bool {
    name.as_encoded_bytes().starts_with(b"UV_")
}

#[cfg(unix)]
pub(crate) use managed_duckdb::resolve_duckdb_extensions;
#[cfg(unix)]
pub(crate) use managed_python::{
    ManagedPython, resolve_python_host, resolve_python_manifest, resolve_python_version,
};
#[cfg(unix)]
pub(crate) use managed_r::{
    ManagedR, ManagedRBootstrap, ManagedRResolverConfiguration, detect_r_bootstrap, resolve_r,
    resolve_r_with,
};
#[cfg(unix)]
pub(crate) use process::ResolverStopHandle;
#[cfg(not(unix))]
pub(crate) use unsupported::{
    ManagedPython, ManagedR, ManagedRBootstrap, ManagedRResolverConfiguration, ResolverStopHandle,
    resolve_duckdb_extensions, resolve_python, resolve_python_host, resolve_python_manifest,
    resolve_python_version, resolve_r, resolve_r_with,
};
