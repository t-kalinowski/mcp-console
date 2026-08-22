#[cfg(target_os = "macos")]
mod platform {
    use std::ffi::{CStr, CString};
    use std::fs;
    use std::io;
    use std::os::unix::ffi::OsStrExt as _;
    use std::os::unix::fs::symlink;
    use std::path::{Path, PathBuf};
    use std::sync::OnceLock;

    use libr::SEXP;

    static MATPLOTLIB_DIRECTORY: OnceLock<PathBuf> = OnceLock::new();
    static INHERITED_MATPLOTLIB_DIRECTORY: OnceLock<PathBuf> = OnceLock::new();

    const PYTHON_BRIDGE_SOURCE: &str = include_str!("python/bridge.R");
    const PYTHON_RUNTIME_SOURCE: &str = include_str!("python/runtime.py");

    #[derive(serde::Deserialize)]
    #[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
    pub(crate) enum PreparationOutcome {
        #[serde(deserialize_with = "crate::worker_protocol::deserialize_payload_free")]
        Prepared,
        Failed {
            message: String,
        },
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

    pub(crate) struct Bridge(crate::r_bridge::Bridge);

    impl Bridge {
        pub(crate) fn initialize() -> Result<Self, String> {
            crate::r_bridge::Bridge::initialize(PYTHON_BRIDGE_SOURCE, "Python").map(Self)
        }

        pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
            self.0.evaluate(source)
        }

        pub(crate) fn prepare(&self, packages: Vec<String>) -> Result<PreparationOutcome, String> {
            let request = serde_json::to_string(&packages)
                .map_err(|error| format!("failed to serialize Python preparation: {error}"))?;
            let response = self
                .0
                .call1_string(c"prepare", &request)?
                .ok_or_else(|| "Python preparation bridge returned no response".to_string())?;
            serde_json::from_str(&response)
                .map_err(|error| format!("invalid Python preparation response: {error}"))
        }
    }

    pub(crate) fn configure_worker_environment() -> io::Result<()> {
        let matplotlib_cache_directory = inherited_matplotlib_directory();
        // Preserve the selected host configuration before redirecting all
        // Matplotlib writes to the worker's private directory.
        if let Some(config) = inherited_matplotlibrc(matplotlib_cache_directory.as_deref()) {
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
            let _ = INHERITED_MATPLOTLIB_DIRECTORY.set(cache);
        }
        link_matplotlib_caches();

        for (name, value, overwrite) in [
            (c"COLUMNS", c"200", true),
            (c"RETICULATE_REMAP_OUTPUT_STREAMS", c"1", true),
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

    fn inherited_matplotlib_directory() -> Option<PathBuf> {
        let directory = match std::env::var_os("MPLCONFIGDIR") {
            Some(directory) if !directory.is_empty() => PathBuf::from(directory),
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

    fn set_environment(name: &CStr, value: &CStr, overwrite: bool) -> io::Result<()> {
        if unsafe { libc::setenv(name.as_ptr(), value.as_ptr(), overwrite.into()) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    // The process-lifetime R bridge calls this once during initialization,
    // before any reticulate hook can initialize Python.
    #[allow(clippy::result_large_err)]
    #[harp::register]
    pub extern "C-unwind" fn mcp_console_python_runtime_source() -> harp::Result<SEXP> {
        Ok(harp::object::RObject::from(PYTHON_RUNTIME_SOURCE).sexp)
    }

    #[allow(clippy::result_large_err)]
    #[harp::register]
    pub extern "C-unwind" fn mcp_console_publish_python_plot(data: SEXP) -> harp::Result<SEXP> {
        let data = String::try_from(harp::object::RObject::view(data))?;
        crate::worker::publish_plot(Ok(data));
        unsafe { Ok(libr::R_NilValue) }
    }

    #[allow(clippy::result_large_err)]
    #[harp::register]
    pub extern "C-unwind" fn mcp_console_resolve_python(request: SEXP) -> harp::Result<SEXP> {
        let request = String::try_from(harp::object::RObject::view(request))?;
        let request = serde_json::from_str(&request).map_err(|error| harp::anyhow!("{error}"))?;
        let python =
            crate::worker::resolve_python(request).map_err(|error| harp::anyhow!("{error}"))?;
        Ok(harp::object::RObject::from(python).sexp)
    }

    #[allow(clippy::result_large_err)]
    #[harp::register]
    pub extern "C-unwind" fn mcp_console_python_activated(
        requirements: SEXP,
    ) -> harp::Result<SEXP> {
        let requirements = String::try_from(harp::object::RObject::view(requirements))?;
        let requirements =
            serde_json::from_str(&requirements).map_err(|error| harp::anyhow!("{error}"))?;
        crate::worker::publish_python_activation(requirements)
            .map_err(|error| harp::anyhow!("{error}"))?;
        unsafe { Ok(libr::R_NilValue) }
    }

    #[allow(clippy::result_large_err)]
    #[harp::register]
    pub extern "C-unwind" fn mcp_console_resolve_python_version(
        request: SEXP,
    ) -> harp::Result<SEXP> {
        let request = String::try_from(harp::object::RObject::view(request))?;
        let request = serde_json::from_str(&request).map_err(|error| harp::anyhow!("{error}"))?;
        let version = crate::worker::resolve_python_version(request)
            .map_err(|error| harp::anyhow!("{error}"))?;
        Ok(harp::object::RObject::from(version).sexp)
    }
}

#[cfg(target_os = "macos")]
pub(crate) use platform::{
    Bridge, PreparationOutcome, configure_worker_environment, link_matplotlib_caches,
};
