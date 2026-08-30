#[cfg(any(target_os = "macos", target_os = "linux"))]
mod reticulate;

#[cfg(any(target_os = "macos", target_os = "linux"))]
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
/// Rust owns the selected interpreter library and initialization, while the
/// current backend delegates conversion and evaluation to reticulate.
#[cfg(any(target_os = "macos", target_os = "linux"))]
pub(crate) struct Runtime(reticulate::Runtime);

#[cfg(any(target_os = "macos", target_os = "linux"))]
pub(crate) fn configure_worker_environment() -> std::io::Result<()> {
    platform::configure_worker_environment()?;
    reticulate::configure_worker_environment()
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
impl Runtime {
    pub(crate) fn initialize() -> Result<Self, String> {
        reticulate::Runtime::initialize().map(Self)
    }

    pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        self.0.evaluate(source)
    }

    pub(crate) fn prepare(&self, packages: Vec<String>) -> Result<PreparationOutcome, String> {
        self.0.prepare(packages)
    }
}

#[cfg(all(test, any(target_os = "macos", target_os = "linux")))]
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

#[cfg(any(target_os = "macos", target_os = "linux"))]
mod library;

#[cfg(any(target_os = "macos", target_os = "linux"))]
mod platform {
    use std::ffi::{CStr, CString};
    use std::fs;
    use std::io;
    use std::os::unix::ffi::OsStrExt as _;
    use std::os::unix::fs::symlink;
    use std::path::{Path, PathBuf};
    use std::sync::OnceLock;

    static MATPLOTLIB_DIRECTORY: OnceLock<PathBuf> = OnceLock::new();
    static INHERITED_MATPLOTLIB_CACHE_DIRECTORY: OnceLock<PathBuf> = OnceLock::new();

    pub(crate) fn configure_worker_environment() -> io::Result<()> {
        let matplotlib_config_directory = inherited_matplotlib_config_directory();
        let matplotlib_cache_directory =
            inherited_matplotlib_cache_directory(matplotlib_config_directory.as_deref());
        // Preserve the selected host configuration before redirecting all
        // Matplotlib writes to the worker's private directory.
        if let Some(config) = inherited_matplotlibrc(matplotlib_config_directory.as_deref()) {
            let config = CString::new(config.as_os_str().as_bytes())
                .expect("Matplotlib configuration path should not contain NUL");
            set_environment(c"MATPLOTLIBRC", &config, true)?;
        }
        let temporary_directory = std::env::temp_dir();
        let matplotlib_directory = temporary_directory.join("matplotlib");
        MATPLOTLIB_DIRECTORY
            .set(matplotlib_directory.clone())
            .map_err(|_| io::Error::other("Matplotlib directory is already configured"))?;
        if let Some(cache) = matplotlib_cache_directory {
            let _ = INHERITED_MATPLOTLIB_CACHE_DIRECTORY.set(cache);
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
            INHERITED_MATPLOTLIB_CACHE_DIRECTORY.get(),
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

    fn inherited_matplotlib_config_directory() -> Option<PathBuf> {
        if let Some(directory) = std::env::var_os("MPLCONFIGDIR").filter(|path| !path.is_empty()) {
            return absolute_directory(PathBuf::from(directory));
        }
        #[cfg(target_os = "macos")]
        let directory = PathBuf::from(std::env::var_os("HOME").filter(|home| !home.is_empty())?)
            .join(".matplotlib");
        #[cfg(target_os = "linux")]
        let directory = std::env::var_os("XDG_CONFIG_HOME")
            .filter(|path| !path.is_empty())
            .map(PathBuf::from)
            .or_else(|| {
                std::env::var_os("HOME")
                    .filter(|home| !home.is_empty())
                    .map(PathBuf::from)
                    .map(|home| home.join(".config"))
            })?
            .join("matplotlib");
        absolute_directory(directory)
    }

    fn inherited_matplotlib_cache_directory(config_directory: Option<&Path>) -> Option<PathBuf> {
        if std::env::var_os("MPLCONFIGDIR").is_some_and(|path| !path.is_empty()) {
            return config_directory.map(Path::to_path_buf);
        }
        #[cfg(target_os = "macos")]
        {
            config_directory.map(Path::to_path_buf)
        }
        #[cfg(target_os = "linux")]
        {
            let root = std::env::var_os("XDG_CACHE_HOME")
                .filter(|path| !path.is_empty())
                .map(PathBuf::from)
                .or_else(|| {
                    std::env::var_os("HOME")
                        .filter(|home| !home.is_empty())
                        .map(PathBuf::from)
                        .map(|home| home.join(".cache"))
                })?;
            absolute_directory(root.join("matplotlib"))
        }
    }

    fn absolute_directory(directory: PathBuf) -> Option<PathBuf> {
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

#[cfg(any(target_os = "macos", target_os = "linux"))]
pub(crate) use platform::link_matplotlib_caches;
