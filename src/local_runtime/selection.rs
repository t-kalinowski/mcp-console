//! Capture one reached Python branch; filesystem absence is the only fallback.
use crate::settings::{Candidate, ManagedPython, Python};
use std::path::{Path, PathBuf};

pub(crate) struct PythonChoice {
    pub explicit: Option<std::ffi::OsString>,
    pub managed: Option<ManagedPython>,
    pub source: String,
}

impl PythonChoice {
    pub(crate) fn capture(configured: Option<Python>) -> Result<Self, String> {
        let Some(configured) = configured else {
            return match std::env::var_os("RETICULATE_PYTHON")
                .filter(|value| !value.is_empty() && value != "managed")
            {
                Some(value) => Ok(Self {
                    explicit: Some(crate::python::explicit_executable(&value)?.into_os_string()),
                    managed: None,
                    source: "RETICULATE_PYTHON".into(),
                }),
                None => Ok(Self::managed(ManagedPython::default(), "default managed")),
            };
        };
        match configured {
            Python::Existing(path) => Self::existing(&path, "python.existing"),
            Python::Managed(options) => Ok(Self::managed(options, "python.managed")),
            Python::FirstAvailable(candidates) => {
                for (index, candidate) in candidates.into_iter().enumerate() {
                    let source = format!("python.first_available[{index}]");
                    match candidate {
                        Candidate::Existing(path) => match std::fs::symlink_metadata(&path) {
                            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                            Err(error) => {
                                return Err(format!(
                                    "{source}: cannot inspect {}: {error}",
                                    path.display()
                                ));
                            }
                            Ok(_) => return Self::existing(&path, &source),
                        },
                        Candidate::ActiveVenv(Some(path)) => {
                            if !std::fs::metadata(&path)
                                .map_err(|error| {
                                    format!("{source}.active_venv {}: {error}", path.display())
                                })?
                                .is_dir()
                            {
                                return Err(format!(
                                    "{source}.active_venv {}: expected a standard venv directory",
                                    path.display()
                                ));
                            }
                            return Self::existing(&path, &format!("{source}.active_venv"));
                        }
                        Candidate::ActiveVenv(None) => continue,
                        Candidate::Managed(options) => return Ok(Self::managed(options, &source)),
                    }
                }
                Err("python.first_available: no candidate is available; supply an existing path or a final managed candidate".into())
            }
        }
    }
    pub(crate) fn validate_environment(
        &self,
        settings: &crate::settings::SandboxSettings,
    ) -> Result<(), String> {
        if self.managed.is_none() {
            return Ok(());
        }
        let Some(values) = settings
            .get("environment")
            .and_then(serde_json::Value::as_object)
        else {
            return Ok(());
        };
        for (name, value) in values {
            let Some(value) = value.as_str() else {
                continue;
            };
            let conflicts = match name.as_str() {
                "RETICULATE_PYTHON" => !value.is_empty() && value != "managed",
                "UV_PYTHON" => !value.is_empty(),
                "UV_PYTHON_PREFERENCE" => value != "only-managed",
                "UV_NO_MANAGED_PYTHON" => !matches!(
                    value.to_ascii_lowercase().as_str(),
                    "0" | "false" | "f" | "no" | "n" | "off"
                ),
                "UV_MANAGED_PYTHON" => !matches!(
                    value.to_ascii_lowercase().as_str(),
                    "1" | "true" | "t" | "yes" | "y" | "on"
                ),
                _ => false,
            };
            if conflicts {
                return Err(format!(
                    "{}: environment.{name} conflicts with managed Python configuration; remove the selection control and use python.managed.version or python.existing",
                    self.source
                ));
            }
        }
        Ok(())
    }
    fn managed(options: ManagedPython, source: &str) -> Self {
        Self {
            explicit: None,
            managed: Some(options),
            source: source.into(),
        }
    }
    fn existing(path: &Path, source: &str) -> Result<Self, String> {
        Ok(Self {
            explicit: Some(
                existing_executable(path)
                    .map_err(|error| format!("{source}: {error}"))?
                    .into_os_string(),
            ),
            managed: None,
            source: source.into(),
        })
    }
}

fn existing_executable(path: &Path) -> Result<PathBuf, String> {
    let metadata = std::fs::metadata(path)
        .map_err(|error| format!("cannot use existing Python {}: {error}", path.display()))?;
    let executable = if metadata.is_dir() {
        reject_conda(path)?;
        let configuration = path.join("pyvenv.cfg");
        if !std::fs::metadata(&configuration).is_ok_and(|metadata| metadata.is_file()) {
            return Err(format!(
                "existing Python directory {} must be a standard venv with pyvenv.cfg",
                path.display()
            ));
        }
        path.join(if cfg!(windows) {
            "Scripts/python.exe"
        } else {
            "bin/python"
        })
    } else if metadata.is_file() {
        let parent = path
            .parent()
            .ok_or("existing Python executable has no parent")?;
        reject_conda(parent)?;
        if let Some(prefix) = parent.parent() {
            reject_conda(prefix)?;
        }
        path.to_path_buf()
    } else {
        return Err(format!(
            "existing Python {} must be an executable or venv directory",
            path.display()
        ));
    };
    if !std::fs::metadata(&executable)
        .map_err(|error| {
            format!(
                "existing Python executable {} is unusable: {error}",
                executable.display()
            )
        })?
        .is_file()
    {
        return Err(format!(
            "existing Python executable {} is not a file",
            executable.display()
        ));
    }
    Ok(executable)
}
fn reject_conda(prefix: &Path) -> Result<(), String> {
    if prefix.join("conda-meta").try_exists().map_err(|error| {
        format!(
            "cannot inspect existing Python {}: {error}",
            prefix.display()
        )
    })? {
        return Err(format!(
            "existing Python {} is a Conda environment; Conda environments are unsupported; use a standard venv or managed Python",
            prefix.display()
        ));
    }
    Ok(())
}
