#[cfg(target_os = "macos")]
mod reticulate;

#[cfg(target_os = "macos")]
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
/// The current implementation delegates to the reticulate backend, while the
/// worker depends only on this facade for evaluation and preparation.
#[cfg(target_os = "macos")]
pub(crate) struct Runtime(reticulate::Runtime);

#[cfg(target_os = "macos")]
pub(crate) fn configure_worker_environment() -> std::io::Result<()> {
    platform::configure_worker_environment()?;
    reticulate::configure_worker_environment()
}

#[cfg(target_os = "macos")]
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

#[cfg(all(test, target_os = "macos"))]
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

#[cfg(target_os = "macos")]
mod library {
    use std::ffi::OsStr;
    use std::path::{Path, PathBuf};
    use std::sync::OnceLock;

    static PYTHON_LIBRARY: OnceLock<LoadedLibrary> = OnceLock::new();

    struct LoadedLibrary {
        identity: LibraryIdentity,
        _library: libloading::os::unix::Library,
    }

    #[derive(Eq, PartialEq)]
    enum LibraryIdentity {
        Process,
        Path(PathBuf),
    }

    pub(super) fn load(path: &Path) -> Result<(), String> {
        let identity = LibraryIdentity::resolve(path)?;
        if let Some(loaded) = PYTHON_LIBRARY.get() {
            return loaded.ensure_identity(&identity);
        }

        let description = identity.description();
        // SAFETY: The selected path comes from reticulate's interpreter
        // discovery. Global, eager loading exposes the CPython API before
        // reticulate initializes the interpreter.
        let library = unsafe { identity.open() }.map_err(|error| {
            format!("failed to load Python shared library `{description}`: {error}")
        })?;

        // SAFETY: Resolving a symbol does not call it. The handle remains owned
        // by `LoadedLibrary` for the process lifetime.
        unsafe {
            library
                .get::<unsafe extern "C" fn() -> libc::c_int>(b"Py_IsInitialized\0")
                .map_err(|error| {
                    format!(
                        "Python shared library `{description}` does not export Py_IsInitialized: {error}"
                    )
                })?;
        }

        let candidate = LoadedLibrary {
            identity,
            _library: library,
        };
        match PYTHON_LIBRARY.set(candidate) {
            Ok(()) => Ok(()),
            Err(candidate) => PYTHON_LIBRARY
                .get()
                .expect("Python library should be set after a concurrent load")
                .ensure_identity(&candidate.identity),
        }
    }

    impl LibraryIdentity {
        fn resolve(path: &Path) -> Result<Self, String> {
            if path.as_os_str() == OsStr::new("NA") {
                return Ok(Self::Process);
            }
            path.canonicalize().map(Self::Path).map_err(|error| {
                format!(
                    "failed to resolve Python shared library `{}`: {error}",
                    path.display()
                )
            })
        }

        unsafe fn open(&self) -> Result<libloading::os::unix::Library, libloading::Error> {
            let flags = libc::RTLD_NOW | libc::RTLD_GLOBAL;
            match self {
                Self::Process => unsafe {
                    libloading::os::unix::Library::open(None::<&OsStr>, flags)
                },
                Self::Path(path) => unsafe {
                    libloading::os::unix::Library::open(Some(path.as_os_str()), flags)
                },
            }
        }

        fn description(&self) -> String {
            match self {
                Self::Process => "current process".to_string(),
                Self::Path(path) => path.display().to_string(),
            }
        }
    }

    impl LoadedLibrary {
        fn ensure_identity(&self, requested: &LibraryIdentity) -> Result<(), String> {
            if self.identity == *requested {
                return Ok(());
            }
            Err(format!(
                "Python shared library is already loaded from `{}` and cannot switch to `{}`",
                self.identity.description(),
                requested.description()
            ))
        }
    }
}

#[cfg(target_os = "macos")]
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

    pub(super) fn set_environment(name: &CStr, value: &CStr, overwrite: bool) -> io::Result<()> {
        if unsafe { libc::setenv(name.as_ptr(), value.as_ptr(), overwrite.into()) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
}

#[cfg(target_os = "macos")]
pub(crate) use platform::link_matplotlib_caches;
