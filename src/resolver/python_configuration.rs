use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

mod managed;

#[derive(Clone)]
pub(crate) struct ManagedPythonResolverConfiguration {
    environment: Arc<BTreeMap<OsString, OsString>>,
    explicit_uv: Option<OsString>,
    reticulate_uv: Option<OsString>,
    uv: Option<OsString>,
    path: Option<OsString>,
    worker_writable: Vec<PathBuf>,
    sans_r: bool,
}

impl ManagedPythonResolverConfiguration {
    pub(crate) fn capture() -> Self {
        let mut environment = std::env::vars_os()
            .filter(|(name, _)| is_uv_environment_variable(name) && name != "UV_OFFLINE")
            .collect::<BTreeMap<_, _>>();
        normalize_python_preference(&mut environment);
        let explicit_uv = std::env::var_os("RETICULATE_UV");
        let reticulate_uv = explicit_uv
            .clone()
            .or_else(|| super::find_path_entry("uv").map(Into::into));
        let uv = reticulate_uv
            .as_ref()
            .filter(|uv| uv.as_os_str() != OsStr::new("managed"))
            .cloned();
        Self {
            environment: Arc::new(environment),
            explicit_uv,
            reticulate_uv,
            uv,
            path: std::env::var_os("PATH"),
            worker_writable: Vec::new(),
            sans_r: false,
        }
    }

    pub(crate) fn without_r_bootstrap(mut self, blocked: &[PathBuf]) -> Result<Self, String> {
        // A prior worker may have replaced a project uv. Select and pin a
        // resolved executable before any resolver process starts.
        self.worker_writable = blocked.iter().map(|root| normalize_path(root)).collect();
        self.sans_r = true;
        self.environment = Arc::new(self.capture_managed_settings()?);
        let blocked = &self.worker_writable;
        let uv = match self.explicit_uv.as_deref() {
            Some(value) if value != OsStr::new("managed") => {
                let path = PathBuf::from(value);
                let path = if path.components().count() == 1 {
                    super::find_path_entry(path.to_str().ok_or("selected uv is not UTF-8")?)
                        .unwrap_or(path)
                } else {
                    path
                };
                Some(select_uv_path(&path, blocked)?.ok_or_else(|| {
                    format!(
                        "selected uv `{}` is in the project or a worker-writable path",
                        path.display()
                    )
                })?)
            }
            _ => find_safe_path_uv(self.path.as_deref(), blocked)?,
        };
        let uv = uv.ok_or("R is unavailable and no protected `uv` executable was found on PATH; install uv or set python in .agents/console/config.yaml to select an existing environment")?;
        self.uv = Some(uv.into());
        self.reticulate_uv = self.uv.clone();
        self.path = Some(self.protected_path(self.path.as_deref())?);
        Ok(self)
    }

    fn protected_path(&self, path: Option<&OsStr>) -> Result<OsString, String> {
        let mut directories = Vec::new();
        if let Some(path) = path {
            for directory in std::env::split_paths(path) {
                if self.path_is_worker_writable(&directory)? {
                    continue;
                }
                // Keep only existing directories so a worker cannot create a
                // previously absent PATH entry or redirect a broken symlink.
                if let Ok(resolved) = directory.canonicalize()
                    && resolved.is_dir()
                {
                    directories.push(resolved);
                }
            }
        }
        std::env::join_paths(directories)
            .map_err(|error| format!("cannot retain protected resolver PATH: {error}"))
    }

    pub(super) fn sans_r(&self) -> bool {
        self.sans_r
    }

    pub(super) fn has_worker_writable_roots(&self) -> bool {
        !self.worker_writable.is_empty()
    }

    pub(super) fn ensure_safe_python_path(&self, path: &Path) -> Result<(), String> {
        if self.path_is_worker_writable(path)? {
            return Err(format!(
                "managed Python path is worker-writable: {}",
                path.display()
            ));
        }
        Ok(())
    }

    fn path_is_worker_writable(&self, path: &Path) -> Result<bool, String> {
        if self.worker_writable.is_empty() {
            return Ok(false);
        }
        let absolute =
            std::path::absolute(path).map_err(|error| format!("cannot locate uv path: {error}"))?;
        let resolved = resolve_path(&absolute)?;
        let absolute = normalize_path(&absolute);
        let resolved = normalize_path(&resolved);
        Ok(self
            .worker_writable
            .iter()
            .any(|root| absolute.starts_with(root) || resolved.starts_with(root)))
    }

    pub(super) fn explicit_uv(&self) -> Option<&OsStr> {
        self.explicit_uv.as_deref()
    }

    pub(super) fn uv(&self) -> Result<&OsStr, String> {
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

    pub(super) fn python_preference(&self) -> Option<&OsStr> {
        self.environment.iter().find_map(|(name, value)| {
            (name.as_os_str() == OsStr::new("UV_PYTHON_PREFERENCE")).then_some(value.as_os_str())
        })
    }

    pub(crate) fn has_uv(&self) -> bool {
        self.uv.is_some()
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
        if self.sans_r {
            command.env("PATH", self.path.as_deref().unwrap_or(OsStr::new("")));
            // The host helper must not execute worker-controlled startup hooks.
            for (name, _) in std::env::vars_os()
                .filter(|(name, _)| name.as_encoded_bytes().starts_with(b"PYTHON"))
            {
                command.env_remove(name);
            }
            command.env_remove("VIRTUAL_ENV").env_remove("CONDA_PREFIX");
        }
    }

    pub(super) fn configure_uv_bootstrap(&self, command: &mut std::process::Command) {
        self.configure_uv(command, OsStr::new("managed"));
    }

    pub(super) fn configure_direct(
        &self,
        command: &mut std::process::Command,
    ) -> Result<(), String> {
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

// Pin existing ancestors too: an intermediate symlink may cross a write grant
// even when its final target is protected. Missing suffixes are created by uv.
fn resolve_path(path: &Path) -> Result<PathBuf, String> {
    let absolute =
        std::path::absolute(path).map_err(|error| format!("cannot locate uv path: {error}"))?;
    let (ancestor, resolved) = absolute
        .ancestors()
        .find_map(|ancestor| {
            ancestor
                .canonicalize()
                .ok()
                .map(|resolved| (ancestor, resolved))
        })
        .ok_or_else(|| format!("cannot resolve uv path: {}", path.display()))?;
    let resolved = resolved.join(absolute.strip_prefix(ancestor).expect("path ancestor"));
    Ok(normalize_path(&resolved))
}

fn normalize_path(path: &Path) -> PathBuf {
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::ParentDir => {
                normalized.pop();
            }
            Component::CurDir => {}
            other => normalized.push(other.as_os_str()),
        }
    }
    normalized
}

fn find_safe_path_uv(path: Option<&OsStr>, blocked: &[PathBuf]) -> Result<Option<PathBuf>, String> {
    let Some(path) = path else {
        return Ok(None);
    };
    for directory in std::env::split_paths(path) {
        let candidate = directory.join("uv");
        if std::fs::symlink_metadata(&candidate).is_ok()
            && let Some(selected) = select_uv_path(&candidate, blocked)?
        {
            return Ok(Some(selected));
        }
    }
    Ok(None)
}

fn select_uv_path(candidate: &Path, blocked: &[PathBuf]) -> Result<Option<PathBuf>, String> {
    // Preserve an existing broken selection outside blocked roots so its
    // resolver error remains visible instead of silently choosing another uv.
    let absolute = std::path::absolute(candidate)
        .map_err(|error| format!("cannot locate selected uv: {error}"))?;
    let selected = candidate.canonicalize().unwrap_or(absolute);
    Ok((!blocked.iter().any(|root| selected.starts_with(root))).then_some(selected))
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
