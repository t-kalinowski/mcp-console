//! Captured runtime selection, retained for every worker generation.

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;
mod r_selection;
mod selection;
pub(crate) use selection::PythonChoice;

#[cfg(any(unix, windows))]
use crate::resolver::{ManagedPython, ResolverStopHandle};

pub(crate) const ENVIRONMENT: &str = "MCP_CONSOLE_LOCAL_RUNTIME";
pub(crate) const DUCKDB_EXTENSION_DIRECTORY: &str = "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY";
#[cfg(not(windows))]
pub(crate) const DEFAULT_DUCKDB_EXTENSIONS: &[&str] = &["icu", "json", "sqlite"];
// SQL is not exposed on Windows, so startup does not load its native adapter.
#[cfg(windows)]
pub(crate) const DEFAULT_DUCKDB_EXTENSIONS: &[&str] = &[];
pub(crate) const RESOLUTION_UNAVAILABLE: &str =
    "dynamic environment resolution is unavailable; install `ir` or `uv` and restart MCP Console";
pub(crate) const LIVE_PREPARATION_DISABLED: &str = "changed requirements other than idle Python package or DuckDB extension additions require control: restart in a Python session without R";

#[derive(Clone, Default, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Selection {
    pub(crate) r_home: Option<PathBuf>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) installation: Option<RInstallation>,
    #[serde(default)]
    pub(crate) r_settings: crate::settings::R,
    #[serde(default)]
    pub(crate) python_resolution: crate::settings::Resolution,
    // In managed sessions, None leaves R declarations and selection hints lazy.
    pub(crate) python: Option<Python>,
}

#[derive(Clone, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Python {
    pub(crate) selected: Box<crate::python::NativePython>,
    pub(crate) explicit: Option<OsString>,
    // The server owns requirements; this is a captured runtime capability.
    pub(crate) managed: bool,
    pub(crate) duckdb_extension_directory: Option<PathBuf>,
}

#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct WorkerSelection {
    pub(crate) r: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) installation: Option<RInstallation>,
    #[serde(default)]
    pub(crate) r_settings: crate::settings::R,
    #[serde(default)]
    pub(crate) python_resolution: crate::settings::Resolution,
    pub(crate) python: Option<Python>,
}

impl Selection {
    pub(crate) fn r_is_present() -> bool {
        // An explicit but invalid R_HOME, or a broken discovered installation,
        // must stay on the R path and report its own failure.
        std::env::var_os("R_HOME").is_some() || crate::resolver::find_r_path_entry().is_some()
    }

    pub(crate) fn python_only(&self) -> bool {
        self.r_home.is_none()
    }

    #[cfg(any(unix, windows))]
    pub(crate) fn prepare_default_duckdb_extensions(
        &self,
        managed: Option<&ManagedPython>,
        resolver: &crate::resolver::execution::PythonConfiguration,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<std::collections::BTreeSet<String>, String> {
        let Some(managed) = managed.filter(|_| !DEFAULT_DUCKDB_EXTENSIONS.is_empty()) else {
            return Ok(Default::default());
        };
        let directory = self
            .duckdb_extension_directory()
            .ok_or("DuckDB extension preparation requires an absolute HOME at server startup")?;
        let extensions = DEFAULT_DUCKDB_EXTENSIONS
            .iter()
            .map(|name| (*name).to_string())
            .collect::<Vec<_>>();
        crate::resolver::execution::resolve_python_duckdb_extensions(
            resolver,
            managed,
            &extensions,
            directory,
            on_started,
        )?;
        Ok(extensions.into_iter().collect())
    }

    pub(crate) fn duckdb_extension_directory(&self) -> Option<&Path> {
        self.python
            .as_ref()
            .and_then(|python| python.duckdb_extension_directory.as_deref())
    }

    pub(crate) fn configure(&self, command: &mut Command) -> Result<(), String> {
        if let Some(installation) = &self.installation {
            installation.configure(command)?;
        }
        if let Some(home) = &self.r_home {
            // Preserve native filename bytes through R_HOME. The structured
            // Python handoff does not need to encode the R path as UTF-8.
            command.env("R_HOME", home);
        }
        command.env(
            ENVIRONMENT,
            serde_json::to_string(&WorkerSelection {
                r: self.r_home.is_some(),
                installation: self.installation.clone(),
                r_settings: self.r_settings.clone(),
                python_resolution: self.python_resolution,
                python: self.python.clone(),
            })
            .map_err(|error| format!("cannot encode runtime selections: {error}"))?,
        );
        if let Some(python) = &self.python {
            command
                .env_remove("PYTHONHOME")
                .env_remove("PYTHONPLATLIBDIR");
            if let Some(explicit) = &python.explicit {
                command.env("RETICULATE_PYTHON", explicit);
            } else if !python.managed {
                command.env("RETICULATE_PYTHON", &python.selected.embedding.python);
            } else if self.r_home.is_some() {
                command.env("RETICULATE_PYTHON", "managed");
            } else {
                command.env_remove("RETICULATE_PYTHON");
            }
            if let Some(directory) = &python.duckdb_extension_directory {
                command.env(DUCKDB_EXTENSION_DIRECTORY, directory);
            } else {
                command.env_remove(DUCKDB_EXTENSION_DIRECTORY);
            }
            if !python.managed {
                command.env_remove("MCP_CONSOLE_MANAGED_PYTHON");
            }
        }
        Ok(())
    }

    pub(crate) fn from_environment() -> Result<Option<WorkerSelection>, String> {
        std::env::var(ENVIRONMENT)
            .ok()
            .map(|value| {
                serde_json::from_str(&value)
                    .map_err(|error| format!("invalid internal local runtime selection: {error}"))
            })
            .transpose()
    }
}

#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub(crate) struct RInstallation {
    pub(crate) home: PathBuf,
    resources: [OsString; 3],
    #[serde(default)]
    identity: Vec<r_selection::FileIdentity>,
    #[serde(default)]
    resource_targets: Vec<PathBuf>,
}

impl RInstallation {
    pub(crate) fn configure_environment(&self) {
        unsafe { std::env::set_var("R_HOME", &self.home) };
        for (name, value) in ["R_SHARE_DIR", "R_INCLUDE_DIR", "R_DOC_DIR"]
            .into_iter()
            .zip(&self.resources)
        {
            unsafe { std::env::set_var(name, value) };
        }
    }
}

#[cfg(unix)]
pub(crate) fn r_installation() -> Result<RInstallation, Box<dyn std::error::Error>> {
    use std::os::unix::ffi::OsStringExt;

    // Harp's setup reads R_HOME with env::var and mistakes non-UTF-8 values
    // for absence. Preserve its validation using the native path in that case.
    let home = if let Some(home) = std::env::var_os("R_HOME").filter(|home| home.to_str().is_none())
    {
        let home = PathBuf::from(home);
        if !home
            .try_exists()
            .map_err(|error| format!("Can't check if `R_HOME` path exists: {error}"))?
        {
            return Err(format!("The `R_HOME` path '{}' does not exist.", home.display()).into());
        }
        home
    } else {
        harp::command::r_home_setup()?
    };
    // Read the selected launcher's resource paths without starting R. Capture
    // them before either interpreter starts; R's install paths can be split.
    let resources = harp::command::r_command(&home, |command| {
        command
            .args([
                "CMD",
                "/bin/sh",
                "-c",
                r#"printf '%s\000' "$R_SHARE_DIR" "$R_INCLUDE_DIR" "$R_DOC_DIR""#,
            ])
            .stdin(std::process::Stdio::null());
    })?;
    if !resources.status.success() {
        return Err(format!(
            "Can't read R resource directories: {}: {}",
            resources.status,
            String::from_utf8_lossy(&resources.stderr)
        )
        .into());
    }
    let values = resources
        .stdout
        .strip_suffix(b"\0")
        .ok_or("R launcher did not terminate its resource directories")?
        .split(|byte| *byte == 0)
        .collect::<Vec<_>>();
    if values.len() != 3 || values.iter().any(|value| value.is_empty()) {
        return Err("R launcher did not supply three resource directories".into());
    }
    let installation = RInstallation {
        home,
        identity: Vec::new(),
        resource_targets: Vec::new(),
        resources: std::array::from_fn(|index| OsString::from_vec(values[index].to_vec())),
    };
    installation.configure_environment();
    Ok(installation)
}

/// Direct launches have no runner-owned private TMPDIR. The relay lifetime
/// retains this directory and removes it only after retiring its worker.
pub(crate) struct TemporaryDirectory(Option<PathBuf>);

impl TemporaryDirectory {
    #[cfg(windows)]
    pub(crate) fn create() -> Result<Self, String> {
        tempfile::Builder::new()
            .prefix("mcp-console-worker-")
            .tempdir()
            .map(|directory| Self(Some(directory.keep())))
            .map_err(|error| format!("cannot create worker temporary directory: {error}"))
    }

    #[cfg(unix)]
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
        let remove = || {
            // Native startup can create read-only directories, as renv does
            // for its base-package view. Only unlock owned temporary
            // directories after retirement; package symlinks stay untouched.
            #[cfg(unix)]
            unlock_temporary_directories(&path)?;
            std::fs::remove_dir_all(&path)
        };
        match remove() {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(format!(
                "cannot remove worker temporary directory {}: {error}",
                path.display()
            )),
        }
    }
}

#[cfg(unix)]
fn unlock_temporary_directories(path: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let metadata = std::fs::symlink_metadata(path)?;
    if !metadata.is_dir() {
        return Ok(());
    }
    std::fs::set_permissions(
        path,
        std::fs::Permissions::from_mode(metadata.permissions().mode() | 0o700),
    )?;
    for entry in std::fs::read_dir(path)? {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            unlock_temporary_directories(&entry.path())?;
        }
    }
    Ok(())
}

impl Drop for TemporaryDirectory {
    fn drop(&mut self) {
        // Pre-launch failures have no relay retirement result to carry errors.
        if let Err(error) = self.retire() {
            eprintln!("{error}");
        }
    }
}

#[cfg(windows)]
pub(crate) fn r_installation() -> Result<RInstallation, Box<dyn std::error::Error>> {
    let home = harp::command::r_home_setup()?;
    let installation = RInstallation {
        identity: Vec::new(),
        resource_targets: Vec::new(),
        resources: ["share", "include", "doc"].map(|name| home.join(name).into_os_string()),
        home,
    };
    installation.configure_environment();
    Ok(installation)
}
