use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

#[derive(Clone)]
pub(crate) struct ManagedPythonResolverConfiguration {
    environment: Arc<BTreeMap<OsString, OsString>>,
    explicit_uv: Option<OsString>,
    reticulate_uv: Option<OsString>,
    uv: Option<OsString>,
    path: Option<OsString>,
    worker_writable: Vec<PathBuf>,
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
        }
    }

    pub(crate) fn without_r_bootstrap(mut self, blocked: &[PathBuf]) -> Result<Self, String> {
        // A prior worker may have replaced a project uv. Select and pin a
        // resolved executable before any resolver process starts.
        self.worker_writable = blocked.iter().map(|root| normalize_path(root)).collect();
        if !self.worker_writable.is_empty() {
            self.path = Some(self.protected_path(self.path.as_deref())?);
            if let Some(search_path) = self
                .environment
                .get(OsStr::new("UV_PYTHON_SEARCH_PATH"))
                .cloned()
            {
                let protected = self.protected_path(Some(&search_path))?;
                Arc::make_mut(&mut self.environment)
                    .insert(OsString::from("UV_PYTHON_SEARCH_PATH"), protected);
            }
        }
        if let Some(variable) = self.worker_writable_configuration()? {
            if self.explicit_uv.is_some() {
                return Err(format!("selected uv cannot use worker-writable {variable}"));
            }
            self.uv = None;
            self.reticulate_uv = None;
            return Ok(self);
        }
        if !self.pin_bare_uv_python()? {
            if self.explicit_uv.is_some() {
                return Err(
                    "selected uv requires UV_PYTHON to resolve to a protected executable"
                        .to_string(),
                );
            }
            self.uv = None;
            self.reticulate_uv = None;
            return Ok(self);
        }
        let blocked = &self.worker_writable;
        if !blocked.is_empty() && !self.environment.contains_key(OsStr::new("UV_CONFIG_FILE")) {
            // A later worker can write project uv.toml files. Keep the host
            // resolver on its captured environment instead of rediscovering them.
            Arc::make_mut(&mut self.environment)
                .insert(OsString::from("UV_NO_CONFIG"), OsString::from("1"));
        }
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
        self.uv = uv.clone().map(Into::into);
        self.reticulate_uv = self.uv.clone();
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
                    && !self.has_worker_writable_python_link(&resolved)?
                {
                    directories.push(resolved);
                }
            }
        }
        std::env::join_paths(directories)
            .map_err(|error| format!("cannot retain protected resolver PATH: {error}"))
    }

    fn has_worker_writable_python_link(&self, directory: &Path) -> Result<bool, String> {
        let Ok(entries) = std::fs::read_dir(directory) else {
            return Ok(true);
        };
        'entries: for entry in entries {
            let entry =
                entry.map_err(|error| format!("cannot inspect Python search path: {error}"))?;
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if !["python", "pypy", "graalpy", "pyodide"]
                .iter()
                .any(|prefix| name.starts_with(prefix))
            {
                continue;
            }
            let mut path = entry.path();
            for _ in 0..32 {
                let Ok(metadata) = std::fs::symlink_metadata(&path) else {
                    return Ok(true);
                };
                if !metadata.file_type().is_symlink() {
                    continue 'entries;
                }
                let Ok(link) = std::fs::read_link(&path) else {
                    return Ok(true);
                };
                path = if link.is_absolute() {
                    link
                } else {
                    path.parent().expect("Python link parent").join(link)
                };
                if self.path_is_worker_writable(&path)? {
                    return Ok(true);
                }
            }
            return Ok(true);
        }
        Ok(false)
    }

    fn pin_bare_uv_python(&mut self) -> Result<bool, String> {
        if self.worker_writable.is_empty() {
            return Ok(true);
        }
        let Some(value) = self.environment.get(OsStr::new("UV_PYTHON")).cloned() else {
            return Ok(true);
        };
        if Path::new(&value).components().count() > 1 || is_abstract_python_request(&value) {
            return Ok(true);
        }
        let search_path = self
            .environment
            .get(OsStr::new("UV_PYTHON_SEARCH_PATH"))
            .map(OsString::as_os_str)
            .or(self.path.as_deref());
        let Some(candidate) = self.find_protected_path_entry_in(&value, search_path)? else {
            return Ok(false);
        };
        let selected = candidate
            .canonicalize()
            .map_err(|error| format!("cannot pin UV_PYTHON executable: {error}"))?;
        Arc::make_mut(&mut self.environment).insert(OsString::from("UV_PYTHON"), selected.into());
        Ok(true)
    }

    fn find_protected_path_entry(&self, program: &OsStr) -> Result<Option<PathBuf>, String> {
        self.find_protected_path_entry_in(program, self.path.as_deref())
    }

    fn find_protected_path_entry_in(
        &self,
        program: &OsStr,
        path: Option<&OsStr>,
    ) -> Result<Option<PathBuf>, String> {
        let Some(path) = path else {
            return Ok(None);
        };
        for directory in std::env::split_paths(path) {
            let candidate = directory.join(program);
            if candidate.canonicalize().is_ok() && !self.path_is_worker_writable(&candidate)? {
                return Ok(Some(candidate));
            }
        }
        Ok(None)
    }

    pub(crate) fn find_path_python(&self) -> Result<Option<PathBuf>, String> {
        if self.worker_writable.is_empty() {
            return Ok(
                super::find_path_entry("python3").or_else(|| super::find_path_entry("python"))
            );
        }
        if let Some(python) = self.find_protected_path_entry(OsStr::new("python3"))? {
            return Ok(Some(python));
        }
        self.find_protected_path_entry(OsStr::new("python"))
    }

    fn worker_writable_configuration(&self) -> Result<Option<&'static str>, String> {
        if self.worker_writable.is_empty() {
            return Ok(None);
        }
        for name in [
            "UV_CACHE_DIR",
            "UV_PYTHON_INSTALL_DIR",
            "UV_PYTHON_CACHE_DIR",
            "UV_TOOL_DIR",
            "UV_CONFIG_FILE",
            "UV_PROJECT_ENVIRONMENT",
        ] {
            if let Some(value) = self.environment.get(OsStr::new(name))
                && self.path_is_worker_writable(Path::new(value))?
            {
                return Ok(Some(name));
            }
        }
        // These inputs can supply build code or interpreter downloads. Parse
        // file URLs before checking paths, including percent-encoded names.
        for (name, delimiter) in [
            ("UV_FIND_LINKS", ','),
            ("UV_INDEX", ' '),
            ("UV_EXTRA_INDEX_URL", ' '),
            ("UV_DEFAULT_INDEX", '\0'),
            ("UV_INDEX_URL", '\0'),
            ("UV_PYTHON_INSTALL_MIRROR", '\0'),
            ("UV_PYPY_INSTALL_MIRROR", '\0'),
            ("UV_PYTHON_DOWNLOADS_JSON_URL", '\0'),
            ("UV_CONSTRAINT", ' '),
            ("UV_BUILD_CONSTRAINT", ' '),
            ("UV_OVERRIDE", ' '),
        ] {
            let Some(value) = self.environment.get(OsStr::new(name)) else {
                continue;
            };
            let value = value
                .to_str()
                .ok_or_else(|| format!("{name} is not UTF-8"))?;
            for source in value
                .split(|c: char| c == delimiter || (name == "UV_INDEX" && c.is_whitespace()))
                .filter(|source| !source.is_empty())
            {
                let source = if matches!(name, "UV_INDEX" | "UV_DEFAULT_INDEX") {
                    source
                        .split_once('=')
                        .filter(|(name, _)| !name.contains(':'))
                        .map_or(source, |(_, url)| url)
                } else {
                    source
                };
                let path = match pep508_rs::VerbatimUrl::parse_url(source) {
                    Ok(url) if url.scheme() == "file" => url
                        .to_file_path()
                        .map_err(|_| format!("{name} has an unsupported local file URL"))?,
                    Ok(url) if pep508_rs::Scheme::parse(url.scheme()).is_some() => continue,
                    _ => PathBuf::from(source),
                };
                if self.path_is_worker_writable(&path)? {
                    return Ok(Some(name));
                }
            }
        }
        if let Some(value) = self.environment.get(OsStr::new("UV_PYTHON"))
            && Path::new(value).components().count() > 1
            && self.path_is_worker_writable(Path::new(value))?
        {
            return Ok(Some("UV_PYTHON"));
        }
        for (name, override_name) in [
            ("XDG_CACHE_HOME", "UV_CACHE_DIR"),
            ("XDG_DATA_HOME", "UV_PYTHON_INSTALL_DIR"),
        ] {
            if !self.environment.contains_key(OsStr::new(override_name))
                && let Some(value) = std::env::var_os(name)
                && self.path_is_worker_writable(Path::new(&value))?
            {
                return Ok(Some(name));
            }
        }
        if let Some(home) = std::env::var_os("HOME")
            && self.path_is_worker_writable(Path::new(&home))?
        {
            return Ok(Some("HOME"));
        }
        Ok(None)
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
        if !self.worker_writable.is_empty() {
            command.env("PATH", self.path.as_deref().unwrap_or(OsStr::new("")));
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

fn is_abstract_python_request(value: &OsStr) -> bool {
    let Some(value) = value.to_str() else {
        return false;
    };
    let value = value.to_ascii_lowercase();
    if matches!(
        value.as_str(),
        "any" | "default" | "cpython" | "pypy" | "graalpy" | "pyodide"
    ) {
        return true;
    }
    let mut version = value.as_str();
    for implementation in ["python", "cpython", "pypy", "graalpy", "pyodide"] {
        if let Some(suffix) = version.strip_prefix(implementation) {
            version = suffix.strip_prefix('@').unwrap_or(suffix);
            break;
        }
    }
    let version = version.strip_suffix('t').unwrap_or(version);
    let components = version.split('.').collect::<Vec<_>>();
    components.len() <= 3
        && components.iter().all(|component| {
            !component.is_empty()
                && component.bytes().all(|byte| byte.is_ascii_digit())
                && component.parse::<u8>().is_ok()
        })
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
