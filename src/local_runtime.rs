//! Captured local runtime selection, retained for every worker generation.

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::resolver::{ManagedPython, ManagedPythonResolverConfiguration, ResolverStopHandle};

pub(crate) const ENVIRONMENT: &str = "MCP_CONSOLE_LOCAL_RUNTIME";
pub(crate) const PREPARATION_DISABLED: &str = "live requirements are unavailable in Python sessions without R; install packages before starting the session";
pub(crate) const IMPORT_DISABLED: &str = "automatic package installation is unavailable in Python sessions without R; install packages before starting the session";

#[derive(serde::Serialize, serde::Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum Selection {
    R {
        home: PathBuf,
    },
    Python {
        selected: Box<crate::python::NativePython>,
        explicit: Option<OsString>,
        // Retain the resolver result as a managed environment, without turning
        // its executable into a RETICULATE_PYTHON user selection.
        managed: Option<ManagedPython>,
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
        resolver: &ManagedPythonResolverConfiguration,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
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
        } else if resolver.has_uv() {
            let managed = crate::resolver::resolve_python_manifest(
                crate::worker_protocol::default_python_requirement_manifest(),
                resolver,
                on_started,
            )?;
            (managed.python().to_path_buf(), Some(managed))
        } else {
            let executable = crate::resolver::find_path_entry("python3")
                .or_else(|| crate::resolver::find_path_entry("python"))
                .ok_or("R is unavailable and neither `uv`, `python3`, nor `python` was found on PATH; install uv or CPython with a shared libpython and restart MCP Console")?;
            (executable, None)
        };
        // Preserve virtualenv symlinks: canonicalizing here would lose the
        // environment even though its base executable has the same identity.
        let executable = std::path::absolute(executable)
            .map_err(|error| format!("cannot locate selected Python: {error}"))?;
        let selected = crate::python::inspect_native(&executable, on_started)?;
        Ok(Self::Python {
            selected: Box::new(selected),
            explicit,
            managed,
        })
    }

    pub(crate) fn python_only(&self) -> bool {
        matches!(self, Self::Python { .. })
    }

    pub(crate) fn configure(&self, command: &mut Command) -> Result<(), String> {
        match self {
            Self::R { home } => {
                // R_HOME already carries the retained selection as a native
                // path; do not require Unix filename bytes to be UTF-8 JSON.
                command.env_remove(ENVIRONMENT);
                command.env("R_HOME", home);
            }
            Self::Python { explicit, .. } => {
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
                command.env_remove("MCP_CONSOLE_MANAGED_PYTHON");
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
pub(crate) struct TemporaryDirectory(PathBuf);

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
        Ok(Self(PathBuf::from(OsString::from_vec(bytes))))
    }

    pub(crate) fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TemporaryDirectory {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}
