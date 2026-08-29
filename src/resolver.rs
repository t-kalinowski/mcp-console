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
        if self.reticulate_uv.is_none() {
            self.reticulate_uv = Some(uv.clone());
            self.uv = Some(uv);
        }
    }

    #[cfg(target_os = "macos")]
    pub(crate) fn set_resolved_uv(&mut self, uv: impl Into<OsString>) {
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
    if environment.contains_key(OsStr::new("UV_PYTHON_PREFERENCE")) {
        return;
    }
    let managed = uv_flag_enabled(environment, "UV_MANAGED_PYTHON");
    let system = uv_flag_enabled(environment, "UV_NO_MANAGED_PYTHON");
    let preference = match (managed, system) {
        (true, false) => "only-managed",
        (false, true) => "only-system",
        (false, false) | (true, true) => return,
    };
    environment.remove(OsStr::new("UV_MANAGED_PYTHON"));
    environment.remove(OsStr::new("UV_NO_MANAGED_PYTHON"));
    environment.insert(
        OsString::from("UV_PYTHON_PREFERENCE"),
        OsString::from(preference),
    );
}

fn uv_flag_enabled(environment: &BTreeMap<OsString, OsString>, name: &str) -> bool {
    environment
        .get(OsStr::new(name))
        .and_then(|value| value.to_str())
        .is_some_and(|value| {
            matches!(
                value.to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
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

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::ffi::{OsStr, OsString};

    use super::normalize_python_preference;

    fn environment(entries: &[(&str, &str)]) -> BTreeMap<OsString, OsString> {
        entries
            .iter()
            .map(|&(name, value)| (OsString::from(name), OsString::from(value)))
            .collect()
    }

    #[test]
    fn normalizes_enabled_uv_python_source_flags() {
        for (name, value, expected) in [
            ("UV_MANAGED_PYTHON", "1", "only-managed"),
            ("UV_NO_MANAGED_PYTHON", "on", "only-system"),
        ] {
            let mut environment = environment(&[(name, value)]);
            normalize_python_preference(&mut environment);
            assert_eq!(
                environment
                    .get(OsStr::new("UV_PYTHON_PREFERENCE"))
                    .map(OsString::as_os_str),
                Some(OsStr::new(expected))
            );
            assert!(!environment.contains_key(OsStr::new("UV_MANAGED_PYTHON")));
            assert!(!environment.contains_key(OsStr::new("UV_NO_MANAGED_PYTHON")));
        }
    }

    #[test]
    fn preserves_conflicting_uv_python_source_configuration() {
        for entries in [
            &[
                ("UV_MANAGED_PYTHON", "1"),
                ("UV_PYTHON_PREFERENCE", "system"),
            ][..],
            &[
                ("UV_MANAGED_PYTHON", "true"),
                ("UV_NO_MANAGED_PYTHON", "true"),
            ][..],
        ] {
            let mut environment = environment(entries);
            let original = environment.clone();
            normalize_python_preference(&mut environment);
            assert_eq!(environment, original);
        }
    }
}
