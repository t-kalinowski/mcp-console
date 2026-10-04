//! Prepared-runtime discovery inside the execution image under workload policy.

use crate::local_runtime::{Python, Selection};
use crate::resolver::preparation::{Discovery, Selections, WorkerEnvironment};
use crate::settings::SandboxSettings;
use serde_json::Value;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;

/// Project only image/workload selectors, never controller runtime settings.
pub(crate) fn configure_probe(command: &mut Command, policy: &SandboxSettings, compute: &str) {
    let inherit = policy
        .get("inherit_environment")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    for name in ["R_HOME", "RETICULATE_PYTHON"] {
        let value = policy
            .get("environment")
            .and_then(|environment| environment.get(name))
            .and_then(Value::as_str)
            .map(OsString::from)
            .or_else(|| inherit.then(|| std::env::var_os(name)).flatten());
        match value {
            Some(value) => {
                command.env(name, value);
            }
            None => {
                command.env_remove(name);
            }
        }
    }
    configure_non_managed(command, compute);
    command.env_remove(crate::local_runtime::ENVIRONMENT);
}

fn configure_non_managed(command: &mut Command, compute: &str) {
    command
        .env("MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION", "0")
        .env("MCP_CONSOLE_EXECUTION_COMPUTE", compute)
        .env("RETICULATE_USE_MANAGED_VENV", "no")
        .env_remove("MCP_CONSOLE_MANAGED_PYTHON")
        .env_remove("MCP_CONSOLE_PREINSTALLED");
}

/// Validate the retained handoff on the execution host before worker readiness.
/// This function must never run on the controller: the paths belong to the image.
pub(crate) fn configure_launch(
    command: &mut Command,
    environment: &WorkerEnvironment,
    compute: &str,
) -> Result<(), String> {
    validate_result(environment)?;
    if let Some(Python { selected, .. }) = environment
        .native
        .as_ref()
        .and_then(|runtime| runtime.python.as_ref())
    {
        for (kind, path) in [
            ("executable", &selected.embedding.python),
            ("embedding library", &selected.embedding.libpython),
        ] {
            if !Path::new(path).is_file() {
                return Err(format!(
                    "retained target Python {kind} no longer exists: {path}"
                ));
            }
        }
        for path in [
            &selected.prefix,
            &selected.exec_prefix,
            &selected.base_prefix,
            &selected.base_exec_prefix,
        ] {
            if !Path::new(path).is_dir() {
                return Err(format!(
                    "retained target Python prefix no longer exists: {path}"
                ));
            }
        }
    }
    environment.configure(command)?;
    // The shared handoff handles CPython layout overrides and R selection.
    // Prepared targets never become managed, even with resolvers in their PATH.
    configure_non_managed(command, compute);
    Ok(())
}

/// Protocol validation only. Target paths stay opaque on the controller.
pub(crate) fn validate_result(environment: &WorkerEnvironment) -> Result<(), String> {
    let discovery = &environment.discovery;
    if discovery.managed
        || environment.r.is_some()
        || environment.python.is_some()
        || discovery.native.is_some()
        || discovery.local_has_uv.is_some()
        || discovery.local_r_home_bytes.is_some()
        || discovery.selections.native_python.is_some()
    {
        return Err("prepared runtime cannot contain managed or preparation state".into());
    }
    let absolute = |path: &str| -> Result<(), String> {
        if !path.starts_with('/') || path.contains('\0') {
            return Err(format!(
                "prepared runtime requires an absolute target path: {path}"
            ));
        }
        Ok(())
    };
    let runtime = environment
        .native
        .as_ref()
        .ok_or("prepared runtime has no captured selections")?;
    if runtime.r_home.as_deref() != discovery.selections.r_home.as_deref().map(Path::new) {
        return Err("prepared runtime R capability differs from its captured selection".into());
    }
    if let Some(home) = &discovery.selections.r_home {
        absolute(home)?;
    }
    if discovery.selections.python.is_some() {
        return Err("prepared Python cannot contain another selection or a managed cache".into());
    }
    let Some(Python {
        selected,
        explicit,
        managed,
        duckdb_extension_directory,
    }) = &runtime.python
    else {
        return if runtime.r_home.is_some() {
            Ok(())
        } else {
            Err("prepared target has neither R nor Python".into())
        };
    };
    if *managed || duckdb_extension_directory.is_some() {
        return Err("prepared Python cannot contain another selection or a managed cache".into());
    }
    for path in [
        &selected.embedding.python,
        &selected.embedding.libpython,
        &selected.prefix,
        &selected.exec_prefix,
        &selected.base_prefix,
        &selected.base_exec_prefix,
    ] {
        absolute(path)?;
    }
    let home = if selected.base_prefix == selected.base_exec_prefix {
        selected.base_prefix.clone()
    } else {
        format!("{}:{}", selected.base_prefix, selected.base_exec_prefix)
    };
    if selected.embedding.python_home != home
        || explicit
            .as_ref()
            .is_some_and(|value| value.to_str() != Some(&selected.embedding.python))
    {
        return Err("prepared Python has inconsistent executable or base prefixes".into());
    }
    Ok(())
}

fn select_python(
    configured: Option<&Path>,
) -> Result<Option<(PathBuf, bool, &'static str)>, String> {
    // Top-level python is target-workspace-relative, including a bare filename.
    // Legacy RETICULATE_PYTHON also accepts an executable name on target PATH.
    let (selected, explicit, source) = if let Some(path) = configured {
        (path.to_path_buf(), true, "python configuration")
    } else if let Some(value) = std::env::var_os("RETICULATE_PYTHON")
        .filter(|value| !value.is_empty() && value != "managed")
    {
        let path = PathBuf::from(value);
        let selected = if path.components().count() == 1 {
            crate::resolver::find_path_entry(
                path.to_str()
                    .ok_or("target Python selection is not UTF-8")?,
            )
            .ok_or("explicit target RETICULATE_PYTHON is not on PATH")?
        } else {
            path
        };
        (selected, true, "RETICULATE_PYTHON")
    } else {
        // Absence alone permits the second name. A broken python3 entry remains
        // selected and must report its validation error rather than try python.
        let Some(selected) = crate::resolver::find_path_entry("python3")
            .or_else(|| crate::resolver::find_path_entry("python"))
        else {
            return Ok(None);
        };
        (selected, false, "PATH Python")
    };
    std::path::absolute(selected)
        .map(|selected| Some((selected, explicit, source)))
        .map_err(|error| format!("cannot locate target Python selection: {error}"))
}

fn discover(configured: Option<&Path>) -> Result<WorkerEnvironment, String> {
    let r_home = if Selection::r_is_present() {
        let home = crate::local_runtime::r_installation()
            .map_err(|error| format!("prepared target R discovery failed: {error}"))?
            .home;
        let library = home.join("lib/libR.so");
        if !library.is_file() {
            return Err("prepared target R requires a shared libR.so; install R with shared-library support in the image or template".into());
        }
        // Loading validates the image's trusted library without initializing R.
        unsafe { libloading::Library::new(library) }
            .map_err(|error| format!("prepared target R library cannot be loaded: {error}"))?;
        Some(
            home.to_str()
                .ok_or("target R home is not UTF-8")?
                .to_owned(),
        )
    } else {
        None
    };
    let python = select_python(configured)?
        .map(|(python, explicit, source)| {
            let selected = crate::python::inspect_native(&python, |_| Ok(()))
                .map_err(|error| format!("prepared target {source} validation failed: {error}"))?;
            let explicit = explicit.then(|| OsString::from(&selected.embedding.python));
            Ok::<_, String>(Python {
                selected: Box::new(selected),
                explicit,
                managed: false,
                duckdb_extension_directory: None,
            })
        })
        .transpose()?;
    let native = Some(Selection {
        r_home: r_home.as_ref().map(PathBuf::from),
        python,
    });
    let environment = WorkerEnvironment {
        discovery: Discovery {
            managed: false,
            selections: Selections {
                r_home,
                python: None,
                native_python: None,
            },
            local_r_home_bytes: None,
            local_has_uv: None,
            native: None,
        },
        r: None,
        python: None,
        native,
    };
    validate_result(&environment)?;
    reject_probe_storage(&environment)?;
    Ok(environment)
}

/// A disposable probe must never retain an executable, library, or prefix
/// produced in its private storage. Resolve these paths only on the target.
fn reject_probe_storage(environment: &WorkerEnvironment) -> Result<(), String> {
    let storage = std::fs::canonicalize(std::env::temp_dir())
        .map_err(|error| format!("cannot locate prepared probe storage: {error}"))?;
    let runtime = environment.native.as_ref().expect("validated selections");
    let mut paths = Vec::new();
    if let Some(python) = &runtime.python {
        let selected = &python.selected;
        paths.extend([
            &selected.embedding.python,
            &selected.embedding.libpython,
            &selected.prefix,
            &selected.exec_prefix,
            &selected.base_prefix,
            &selected.base_exec_prefix,
        ]);
    }
    if let Some(home) = &environment.discovery.selections.r_home {
        paths.push(home);
    }
    for path in paths {
        let resolved = std::fs::canonicalize(path)
            .map_err(|error| format!("prepared runtime path is unusable: {path}: {error}"))?;
        if resolved.starts_with(&storage) {
            return Err(format!(
                "prepared runtime path belongs to disposable probe storage: {path}"
            ));
        }
    }
    Ok(())
}

/// The inspector uses its descriptor-owned result file, not Python stdout.
/// Only this trusted entry point emits the bounded target protocol frame.
pub(crate) fn runtime_probe(configured: Option<&Path>) -> Result<(), String> {
    let environment = discover(configured)?;
    write_result(&mut std::io::stdout().lock(), &environment)
}

/// Keep the bound when an owner re-encodes a validated inner result.
pub(crate) fn write_result(
    writer: &mut impl std::io::Write,
    environment: &WorkerEnvironment,
) -> Result<(), String> {
    let bytes = serde_json::to_vec(&environment).map_err(|error| error.to_string())?;
    if bytes.len() > super::MAX_FRAME {
        return Err("prepared runtime result exceeds 64 KiB".into());
    }
    super::write_frame(writer, super::RUNTIME, &bytes)
        .map_err(|error| format!("cannot report prepared runtime: {error}"))
}
