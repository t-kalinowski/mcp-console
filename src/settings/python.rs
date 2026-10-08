//! A captured existing path or a bounded Python-only candidate list.

use serde::{Deserialize, de::Error as _};
use serde_json::Value;
use std::path::{Path, PathBuf};

#[derive(Clone)]
pub(crate) struct PythonChoice {
    pub(crate) executable: Option<PathBuf>,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum Mapping {
    Existing(PathBuf),
    Managed(Managed),
    FirstAvailable(Vec<Value>),
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Managed {}

pub(super) struct Python(Value);

impl<'de> Deserialize<'de> for Python {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = Value::deserialize(deserializer)?;
        if value.is_string() || value.is_object() {
            Ok(Self(value))
        } else {
            Err(D::Error::custom(
                "python: expected an existing path or a selection mapping",
            ))
        }
    }
}

enum Candidate {
    Existing(PathBuf),
    ActiveVenv,
    Managed,
}

impl Python {
    pub(super) fn capture(self) -> Result<PythonChoice, String> {
        if let Some(path) = self.0.as_str() {
            return existing(&capture_path(Path::new(path))?);
        }
        match mapping(self.0)? {
            Mapping::Existing(path) => existing(&capture_path(&path)?),
            Mapping::Managed(_) => Ok(PythonChoice { executable: None }),
            Mapping::FirstAvailable(values) => {
                if values.is_empty() {
                    return Err("python.first_available must not be empty".into());
                }
                let count = values.len();
                let mut active = false;
                let mut candidates = Vec::with_capacity(count);
                for (index, value) in values.into_iter().enumerate() {
                    let candidate = if value == "active_venv" && !active {
                        active = true;
                        Candidate::ActiveVenv
                    } else {
                        match mapping(value)? {
                            Mapping::Existing(path) => Candidate::Existing(capture_path(&path)?),
                            Mapping::Managed(_) if index + 1 == count => Candidate::Managed,
                            _ => return Err("python.first_available permits existing paths, one active_venv, and one optional managed candidate last; nested chains are unsupported".into()),
                        }
                    };
                    candidates.push(candidate);
                }
                for candidate in candidates {
                    match candidate {
                        Candidate::Existing(path) => match std::fs::symlink_metadata(&path) {
                            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                            _ => return existing(&path),
                        },
                        Candidate::ActiveVenv => {
                            let Some(path) =
                                std::env::var_os("VIRTUAL_ENV").filter(|path| !path.is_empty())
                            else {
                                continue;
                            };
                            let path = capture_path(Path::new(&path))?;
                            if !std::fs::metadata(&path)
                                .map_err(|error| {
                                    format!("python.active_venv {}: {error}", path.display())
                                })?
                                .is_dir()
                            {
                                return Err(
                                    "python.active_venv must name a standard venv directory".into(),
                                );
                            }
                            return existing(&path);
                        }
                        Candidate::Managed => return Ok(PythonChoice { executable: None }),
                    }
                }
                Err("python.first_available: no candidate is available".into())
            }
        }
    }
}

fn mapping(value: Value) -> Result<Mapping, String> {
    if !value.is_object() || value.as_object().is_some_and(|value| value.len() != 1) {
        return Err("python: expected exactly one of existing, managed, first_available; managed accepts an empty mapping".into());
    }
    serde_path_to_error::deserialize(value).map_err(|error| format!("python: {error}"))
}

fn capture_path(path: &Path) -> Result<PathBuf, String> {
    if path.as_os_str().is_empty() {
        return Err("python: expected a nonempty path".into());
    }
    let path = if let Ok(relative) = path.strip_prefix("~") {
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .filter(|home| home.is_absolute())
            .ok_or("configured Python home expansion requires an absolute HOME")?;
        home.join(relative)
    } else {
        path.to_path_buf()
    };
    std::path::absolute(path).map_err(|error| format!("cannot locate configured Python: {error}"))
}

fn existing(path: &Path) -> Result<PythonChoice, String> {
    let metadata = std::fs::metadata(path)
        .map_err(|error| format!("cannot use existing Python {}: {error}", path.display()))?;
    let executable = if metadata.is_dir() {
        reject_conda(path)?;
        if !path.join("pyvenv.cfg").is_file() {
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
        for prefix in path
            .parent()
            .into_iter()
            .chain(path.parent().and_then(Path::parent))
        {
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
    Ok(PythonChoice {
        executable: Some(executable),
    })
}

fn reject_conda(prefix: &Path) -> Result<(), String> {
    if prefix
        .join("conda-meta")
        .try_exists()
        .map_err(|error| error.to_string())?
    {
        return Err(format!(
            "existing Python {} is a Conda environment; Conda environments are unsupported; use a standard venv or managed Python",
            prefix.display()
        ));
    }
    Ok(())
}
