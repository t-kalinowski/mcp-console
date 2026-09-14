mod native;
mod reticulate;
pub(crate) use reticulate::Interop;

const RUNTIME_SOURCE: &str = include_str!("python/runtime.py");

#[derive(serde::Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum PreparationOutcome {
    #[serde(deserialize_with = "crate::worker_protocol::deserialize_payload_free")]
    Prepared,
    Failed {
        message: String,
    },
}

/// Rust-owned Python runtime boundary.
///
/// Console owns interpreter selection, initialization, the evaluator, and
/// environment activation. Reticulate attaches only for R/Python conversion.
pub(crate) struct Runtime {
    next_evaluation_id: u64,
}

pub(crate) enum SqlProvider {
    R,
    Managed,
    Handled,
}

pub(crate) fn configure_worker_environment(
    temporary_directory: &std::path::Path,
    managed_r: bool,
) -> std::io::Result<()> {
    platform::configure_worker_environment(temporary_directory)?;
    native::configure(temporary_directory, managed_r).map_err(std::io::Error::other)
}

impl Runtime {
    pub(crate) fn initialize() -> Result<Self, String> {
        Ok(Self {
            next_evaluation_id: 1,
        })
    }

    pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        if let Err(message) = native::ensure_initialized() {
            crate::worker::emit_diagnostic(&format!("Error: {message}\n"));
            return Ok(());
        }
        let filename = format!("<mcp-console:python:e{}>", self.next_evaluation_id);
        self.next_evaluation_id += 1;
        library::call_json(
            c"_mcp_console_environment",
            c"evaluate",
            &serde_json::json!({
                "source": source, "filename": filename,
            }),
        )
        .map(|_| ())
    }

    pub(crate) fn prepare(&self, packages: Vec<String>) -> Result<PreparationOutcome, String> {
        let result = native::prepare(serde_json::json!({"packages": packages}))?;
        serde_json::from_value(result).map_err(|error| error.to_string())
    }
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

pub(crate) fn ensure_initialized() -> Result<(), String> {
    native::ensure_initialized()
}
