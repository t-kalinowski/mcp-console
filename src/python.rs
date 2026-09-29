mod inspection;
mod requirements;
mod reticulate;
mod startup;

pub(crate) use inspection::{NativePython, explicit_executable, inspect_native};
pub(crate) use requirements::{ActivationFailure, ensure_libpython_compatible};
#[cfg(test)]
pub(crate) use requirements::{ActivationInput, activate_managed_environment};
pub(crate) use startup::{finish_initialization, initialize_selected, setup_runtime};

const RUNTIME_SOURCE: &str = include_str!("python/runtime.py");

/// Import policy installed before shared runtime setup is complete.
pub(crate) enum ImportResolution<'a> {
    Managed,
    Disabled(&'a str),
}

#[derive(serde::Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum PreparationOutcome {
    #[serde(deserialize_with = "crate::worker_protocol::deserialize_payload_free")]
    Prepared,
    Failed {
        message: String,
    },
    Rejected {
        message: String,
    },
}

/// Rust-owned Python runtime boundary.
///
/// Every cell enters the same private evaluator through the retained CPython
/// library. The optional reticulate adapter preserves startup declarations,
/// hooks, conversion, and events for the same native selection.
pub(crate) struct Runtime {
    next_evaluation_id: u64,
}

pub(crate) enum SqlProvider {
    R,
    Managed,
    Handled,
}

pub(crate) use platform::configure_worker_environment as configure_native_worker_environment;

pub(crate) use reticulate::{
    configure_worker_environment as configure_r_environment, defer_r_startup, finish_r_startup,
};

thread_local! {
    static ADAPTER: std::cell::RefCell<Option<std::rc::Rc<reticulate::Adapter>>> = const { std::cell::RefCell::new(None) };
}
static SELECTION: std::sync::OnceLock<crate::local_runtime::WorkerSelection> =
    std::sync::OnceLock::new();

pub(crate) fn attach_r_adapter() -> Result<(), String> {
    if let Some(manifest) = requirements::retained_manifest() {
        unsafe {
            std::env::set_var(
                "MCP_CONSOLE_MANAGED_PYTHON",
                serde_json::to_string(&manifest).map_err(|error| error.to_string())?,
            )
        };
    }
    let adapter = std::rc::Rc::new(reticulate::Adapter::initialize()?);
    ADAPTER.with(|slot| *slot.borrow_mut() = Some(adapter));
    Ok(())
}

fn adapter() -> Option<std::rc::Rc<reticulate::Adapter>> {
    ADAPTER.with(|slot| slot.borrow().clone())
}

pub(crate) fn attach_bridge() -> Result<bool, String> {
    adapter()
        .ok_or("R bridge is unavailable")?
        .ensure_initialized()
}

pub(crate) fn reinstall_services() -> Result<(), String> {
    if library::initialized_selection()?.is_some() && library::services_installed()? {
        library::install_services()?;
    }
    Ok(())
}

pub(crate) fn bridge_available() -> Result<bool, String> {
    if !available() {
        return Ok(false);
    }
    adapter().map_or(Ok(false), |adapter| adapter.available())
}

pub(crate) fn available() -> bool {
    SELECTION
        .get()
        .is_some_and(|selection| selection.python.is_some())
}

pub(crate) fn ensure_initialized() -> Result<bool, String> {
    if library::runtime_configured()? {
        return Ok(true);
    }
    if let Some(candidate) = requirements::materialized() {
        return startup::initialize_native(&candidate.selected, true);
    }
    let python = SELECTION
        .get()
        .and_then(|selection| selection.python.as_ref())
        .ok_or("Python is unavailable in this session")?;
    startup::initialize_native(&python.selected, python.managed)
}

impl Runtime {
    pub(crate) fn initialize(&self) -> Result<(), String> {
        if bridge_available()?
            && let Some(adapter) = adapter()
            && adapter.select()?.is_none()
        {
            return Err("Python startup selection did not complete; restart required".into());
        }
        if available() && !ensure_initialized()? {
            return Err("Python initialization did not complete; restart required".into());
        }
        Ok(())
    }

    pub(crate) fn new(selection: crate::local_runtime::WorkerSelection) -> Result<Self, String> {
        requirements::configure()?;
        SELECTION
            .set(selection)
            .map_err(|_| "runtime capabilities already configured")?;
        Ok(Self {
            next_evaluation_id: 1,
        })
    }

    pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        let filename = format!("<mcp-console:python:e{}>", self.next_evaluation_id);
        self.next_evaluation_id += 1;
        if !available() {
            return Err("Python is unavailable in this session".into());
        }
        evaluate_embedded(source, &filename)
    }

    pub(crate) fn prepare(&self, packages: Vec<String>) -> Result<PreparationOutcome, String> {
        requirements::prepare(packages)
    }
}

pub(crate) fn resolve_managed_import(
    resolution: crate::worker_protocol::PythonImportResolution,
) -> Result<String, String> {
    requirements::resolve_import(resolution)
}

pub(crate) fn evaluate_embedded(source: &str, filename: &str) -> Result<(), String> {
    library::evaluate(source, filename)
}

pub(crate) fn initialize_managed_sql() -> Result<(), String> {
    library::initialize_managed_sql()
}

pub(crate) fn install_sql_runtime(source: &str) -> Result<(), String> {
    library::install_sql_runtime(source)
}

pub(crate) fn dispatch_sql(source: &str) -> Result<SqlProvider, String> {
    library::dispatch_sql(source)
}

pub(crate) fn use_r_sql() -> Result<(), String> {
    library::use_r_sql()
}

pub(crate) fn take_sql_restore_request() -> Result<bool, String> {
    library::take_sql_restore_request()
}

pub(crate) fn prepare_process_exit() -> Result<(), String> {
    library::prepare_process_exit()
}

#[cfg(test)]
mod tests {
    use super::PreparationOutcome;

    #[test]
    fn python_preparation_outcome_rejects_unknown_fields() {
        assert!(serde_json::from_str::<PreparationOutcome>(r#"{"kind":"prepared"}"#).is_ok());
        assert!(
            serde_json::from_str::<PreparationOutcome>(
                r#"{"kind":"prepared","checkpoint":{"packages":[]}}"#
            )
            .is_err()
        );
    }
}

mod library;

mod platform {
    use std::ffi::{CStr, CString};
    use std::fs;
    use std::io;
    use std::os::unix::ffi::OsStrExt as _;
    use std::os::unix::fs::symlink;
    use std::path::{Path, PathBuf};
    use std::sync::OnceLock;

    static MATPLOTLIB_DIRECTORY: OnceLock<PathBuf> = OnceLock::new();
    static INHERITED_MATPLOTLIB_DIRECTORY: OnceLock<PathBuf> = OnceLock::new();

    pub(crate) fn configure_worker_environment(temporary_directory: &Path) -> io::Result<()> {
        let matplotlib_cache_directory = inherited_matplotlib_directory("XDG_CACHE_HOME", ".cache");
        let matplotlib_config_directory =
            inherited_matplotlib_directory("XDG_CONFIG_HOME", ".config");
        // Preserve the selected host configuration before redirecting all
        // Matplotlib writes to the worker's private directory.
        if let Some(config) = inherited_matplotlibrc(matplotlib_config_directory.as_deref()) {
            let config = CString::new(config.as_os_str().as_bytes())
                .expect("Matplotlib configuration path should not contain NUL");
            set_environment(c"MATPLOTLIBRC", &config, true)?;
        }
        let matplotlib_directory = temporary_directory.join("matplotlib");
        MATPLOTLIB_DIRECTORY
            .set(matplotlib_directory.clone())
            .map_err(|_| io::Error::other("Matplotlib directory is already configured"))?;
        if let Some(cache) = matplotlib_cache_directory {
            let _ = INHERITED_MATPLOTLIB_DIRECTORY.set(cache);
        }
        link_matplotlib_caches();

        for (name, value, overwrite) in [
            (c"COLUMNS", c"200", true),
            (c"UV_OFFLINE", c"1", true),
            (c"MPLBACKEND", c"agg", false),
        ] {
            set_environment(name, value, overwrite)?;
        }

        for (name, directory) in [
            (c"MPLCONFIGDIR", matplotlib_directory),
            (c"XDG_CACHE_HOME", temporary_directory.join("cache")),
        ] {
            let directory = CString::new(directory.as_os_str().as_bytes())
                .expect("temporary directory should not contain NUL");
            set_environment(name, &directory, true)?;
        }
        Ok(())
    }

    pub(crate) fn link_matplotlib_caches() {
        let (Some(cache_directory), Some(directory)) = (
            INHERITED_MATPLOTLIB_DIRECTORY.get(),
            MATPLOTLIB_DIRECTORY.get(),
        ) else {
            return;
        };
        let Ok(caches) = fs::read_dir(cache_directory) else {
            return;
        };
        if fs::create_dir_all(directory).is_err() {
            return;
        }
        for cache in caches.flatten() {
            let name = cache.file_name();
            let Some(name) = name.to_str() else {
                continue;
            };
            if !name.starts_with("fontlist-v")
                || !name.ends_with(".json")
                || !cache.file_type().is_ok_and(|file_type| file_type.is_file())
            {
                continue;
            }
            let link = directory.join(name);
            if fs::symlink_metadata(&link).is_err() {
                let _ = symlink(cache.path(), link);
            }
        }
    }

    fn inherited_matplotlibrc(config_directory: Option<&Path>) -> Option<PathBuf> {
        if let Some(config) = std::env::var_os("MATPLOTLIBRC").filter(|path| !path.is_empty()) {
            let config = PathBuf::from(config);
            if let Some(config) =
                regular_file(&config).or_else(|| regular_file(&config.join("matplotlibrc")))
            {
                return Some(config);
            }
        }

        regular_file(&config_directory?.join("matplotlibrc"))
    }

    fn inherited_matplotlib_directory(xdg_variable: &str, xdg_default: &str) -> Option<PathBuf> {
        let directory = match std::env::var_os("MPLCONFIGDIR") {
            Some(directory) if !directory.is_empty() => PathBuf::from(directory),
            Some(_) | None if cfg!(target_os = "linux") => {
                let root = std::env::var_os(xdg_variable)
                    .filter(|path| !path.is_empty())
                    .map(PathBuf::from)
                    .or_else(|| {
                        std::env::var_os("HOME")
                            .filter(|home| !home.is_empty())
                            .map(|home| PathBuf::from(home).join(xdg_default))
                    })?;
                root.join("matplotlib")
            }
            Some(_) | None => {
                PathBuf::from(std::env::var_os("HOME").filter(|home| !home.is_empty())?)
                    .join(".matplotlib")
            }
        };
        if directory.is_absolute() {
            Some(directory)
        } else {
            Some(std::env::current_dir().ok()?.join(directory))
        }
    }

    fn regular_file(path: &Path) -> Option<PathBuf> {
        let path = path.canonicalize().ok()?;
        path.is_file().then_some(path)
    }

    pub(super) fn set_environment(name: &CStr, value: &CStr, overwrite: bool) -> io::Result<()> {
        if unsafe { libc::setenv(name.as_ptr(), value.as_ptr(), overwrite.into()) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
}

pub(crate) use platform::link_matplotlib_caches;
