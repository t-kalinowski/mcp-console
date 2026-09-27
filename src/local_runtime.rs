//! Captured runtime selection, retained for every worker generation.

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::resolver::{ManagedPython, ResolverStopHandle};

pub(crate) const ENVIRONMENT: &str = "MCP_CONSOLE_LOCAL_RUNTIME";
pub(crate) const DUCKDB_EXTENSION_DIRECTORY: &str = "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY";
pub(crate) const PREPARATION_DISABLED: &str = "Python requirements are unavailable in this non-managed Python session; install packages before starting the session";
pub(crate) const LIVE_PREPARATION_DISABLED: &str = "changed requirements other than idle Python package or DuckDB extension additions require control: restart in a Python session without R";
pub(crate) const IMPORT_DISABLED: &str = "automatic package installation is unavailable in Python sessions without R; install packages before starting the session";
pub(crate) const MANAGED_IMPORT_DISABLED: &str = "automatic package installation is unavailable in Python sessions without R; add a new distribution with requirements.python in an idle session, or use control: restart to replace a declaration";

#[derive(Clone, serde::Serialize, serde::Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum Selection {
    R {
        home: PathBuf,
    },
    Python {
        selected: Box<crate::python::NativePython>,
        explicit: Option<OsString>,
        // Capability only. The session environment owns the retained manifest.
        managed: bool,
        // DuckDB's host cache is shared across worker generations. Its path
        // must not follow the runner's disposable TMPDIR or mutable worker HOME.
        duckdb_extension_directory: Option<PathBuf>,
    },
}

impl Selection {
    pub(crate) fn r_is_present() -> bool {
        // An explicit but invalid R_HOME, or a broken discovered installation,
        // must stay on the R path and report its own failure.
        std::env::var_os("R_HOME").is_some() || crate::resolver::find_path_entry("R").is_some()
    }

    pub(crate) fn python(
        configured: Option<OsString>,
        resolver: &crate::resolver::execution::PythonConfiguration,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, Option<ManagedPython>), String> {
        Self::python_with(
            configured,
            resolver.has_uv(),
            |started| {
                crate::resolver::execution::resolve_python_manifest(
                    crate::worker_protocol::default_native_python_requirement_manifest(),
                    resolver,
                    None,
                    None,
                    started,
                )
            },
            |executable, started| {
                crate::resolver::execution::inspect_native(resolver, executable, started)
            },
            on_started,
        )
    }

    pub(crate) fn python_on_host(
        configured: Option<OsString>,
        resolver: &crate::resolver::ManagedPythonResolverConfiguration,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, Option<ManagedPython>), String> {
        let (mut selection, managed) = Self::python_with(
            configured,
            resolver.has_uv(),
            |started| {
                crate::resolver::resolve_python_manifest_for_remote(
                    crate::worker_protocol::default_native_python_requirement_manifest(),
                    resolver,
                    None,
                    None,
                    started,
                )
            },
            |executable, started| crate::python::inspect_native(executable, started),
            on_started,
        )?;
        if let Self::Python {
            selected, explicit, ..
        } = &mut selection
            && explicit.is_some()
        {
            *explicit = Some(OsString::from(&selected.embedding.python));
        }
        Ok((selection, managed))
    }

    fn python_with(
        configured: Option<OsString>,
        has_uv: bool,
        resolve: impl FnOnce(
            &dyn Fn(ResolverStopHandle) -> Result<(), String>,
        ) -> Result<ManagedPython, String>,
        inspect: impl FnOnce(
            &Path,
            &dyn Fn(ResolverStopHandle) -> Result<(), String>,
        ) -> Result<crate::python::NativePython, String>,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, Option<ManagedPython>), String> {
        let explicit = configured.filter(|value| !value.is_empty() && value != "managed");
        let (executable, managed) = if let Some(explicit) = &explicit {
            let executable = PathBuf::from(explicit);
            let executable = if executable.components().count() == 1 {
                crate::resolver::find_path_entry(
                    executable
                        .to_str()
                        .ok_or("explicit Python executable is not UTF-8")?,
                )
                .ok_or("explicit Python executable is not on PATH")?
            } else {
                executable
            };
            (executable, None)
        } else {
            if !has_uv {
                return Err("Python sessions without R require `uv` on PATH; set python in .agents/console/config.yaml to use an existing environment".into());
            }
            let managed = resolve(on_started)?;
            (managed.python().to_path_buf(), Some(managed))
        };
        // Preserve virtualenv symlinks: canonicalizing here would lose the
        // environment even though its base executable has the same identity.
        let executable = std::path::absolute(executable)
            .map_err(|error| format!("cannot locate selected Python: {error}"))?;
        let selected = inspect(&executable, on_started)?;
        // Ordinary managed Python sessions also work without HOME. A shared
        // extension cache is required only when extensions are requested.
        let duckdb_extension_directory = managed.as_ref().and_then(|_| {
            std::env::var_os("HOME")
                .map(PathBuf::from)
                .filter(|home| home.is_absolute())
                .map(|home| home.join(".duckdb/extensions"))
        });
        let selection = Self::Python {
            selected: Box::new(selected),
            explicit,
            managed: managed.is_some(),
            duckdb_extension_directory,
        };
        Ok((selection, managed))
    }

    pub(crate) fn python_only(&self) -> bool {
        matches!(self, Self::Python { .. })
    }

    pub(crate) fn duckdb_extension_directory(&self) -> Option<&Path> {
        match self {
            Self::Python {
                duckdb_extension_directory,
                ..
            } => duckdb_extension_directory.as_deref(),
            Self::R { .. } => None,
        }
    }

    pub(crate) fn configure(&self, command: &mut Command) -> Result<(), String> {
        match self {
            Self::R { home } => {
                // R_HOME already carries the retained selection as a native
                // path; do not require Unix filename bytes to be UTF-8 JSON.
                command.env_remove(ENVIRONMENT);
                command.env("R_HOME", home);
            }
            Self::Python {
                explicit,
                managed,
                duckdb_extension_directory,
                ..
            } => {
                command.env(
                    ENVIRONMENT,
                    serde_json::to_string(self).map_err(|error| {
                        format!("cannot encode local runtime selection: {error}")
                    })?,
                );
                // Inspection ignores Python layout overrides. Keep embedding
                // and children on that selection after sandbox projection too.
                command
                    .env_remove("PYTHONHOME")
                    .env_remove("PYTHONPLATLIBDIR");
                if let Some(python) = explicit {
                    command.env("RETICULATE_PYTHON", python);
                } else {
                    command.env_remove("RETICULATE_PYTHON");
                }
                if let Some(directory) = duckdb_extension_directory {
                    command.env(DUCKDB_EXTENSION_DIRECTORY, directory);
                } else {
                    command.env_remove(DUCKDB_EXTENSION_DIRECTORY);
                }
                if !*managed {
                    command.env_remove("MCP_CONSOLE_MANAGED_PYTHON");
                }
            }
        }
        Ok(())
    }

    pub(crate) fn from_environment() -> Result<Option<Self>, String> {
        std::env::var(ENVIRONMENT)
            .ok()
            .map(|value| {
                serde_json::from_str(&value)
                    .map_err(|error| format!("invalid internal local runtime selection: {error}"))
            })
            .transpose()
    }
}

pub(crate) fn r_home() -> Result<PathBuf, Box<dyn std::error::Error>> {
    // Harp's setup reads R_HOME with env::var and mistakes non-UTF-8 values
    // for absence. Preserve its validation using the native path in that case.
    let Some(home) = std::env::var_os("R_HOME").filter(|home| home.to_str().is_none()) else {
        return Ok(harp::command::r_home_setup()?);
    };
    let home = PathBuf::from(home);
    if !home
        .try_exists()
        .map_err(|error| format!("Can't check if `R_HOME` path exists: {error}"))?
    {
        return Err(format!("The `R_HOME` path '{}' does not exist.", home.display()).into());
    }
    harp::command::r_command(&home, |command| {
        command.arg("RHOME");
    })
    .map_err(|error| format!("Can't run R: {error}"))?;
    Ok(home)
}

/// Direct launches have no runner-owned private TMPDIR. The relay lifetime
/// retains this directory and removes it only after retiring its worker.
pub(crate) struct TemporaryDirectory(Option<PathBuf>);

impl TemporaryDirectory {
    pub(crate) fn create() -> Result<Self, String> {
        use std::os::unix::ffi::{OsStrExt, OsStringExt};
        let template = std::env::temp_dir().join("mcp-console-worker-XXXXXX");
        let mut bytes = template.as_os_str().as_bytes().to_vec();
        bytes.push(0);
        // mkdtemp creates a private, unique directory with mode 0700.
        if unsafe { libc::mkdtemp(bytes.as_mut_ptr().cast()) }.is_null() {
            return Err(format!(
                "cannot create worker temporary directory: {}",
                std::io::Error::last_os_error()
            ));
        }
        bytes.pop();
        Ok(Self(Some(PathBuf::from(OsString::from_vec(bytes)))))
    }

    pub(crate) fn path(&self) -> &Path {
        self.0.as_deref().expect("temporary directory not retired")
    }

    pub(crate) fn retire(&mut self) -> Result<(), String> {
        let Some(path) = self.0.take() else {
            return Ok(());
        };
        match std::fs::remove_dir_all(&path) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(format!(
                "cannot remove worker temporary directory {}: {error}",
                path.display()
            )),
        }
    }
}

impl Drop for TemporaryDirectory {
    fn drop(&mut self) {
        // Pre-launch failures have no relay retirement result to carry errors.
        if let Err(error) = self.retire() {
            eprintln!("{error}");
        }
    }
}
