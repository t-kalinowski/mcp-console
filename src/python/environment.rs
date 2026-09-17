//! One worker-owned manifest, with lazy declarations and provisional activation.

use std::cell::RefCell;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use super::{PreparationOutcome, library, probe};
use crate::worker;
use crate::worker_protocol::{
    PythonImportResolution, PythonRequirementManifest, PythonResolveRequest,
};

#[derive(Default)]
struct State {
    accepted: Option<PythonRequirementManifest>,
    declared: Option<PythonRequirementManifest>,
    provisional: Option<PythonRequirementManifest>,
    active: Option<Value>,
}

thread_local! {
    static STATE: RefCell<State> = RefCell::new(State::default());
}

pub(super) fn initialize() -> Result<(), String> {
    // Capture before the startup bridge clears the launch environment.
    let accepted = std::env::var("MCP_CONSOLE_MANAGED_PYTHON")
        .ok()
        .map(|seed| serde_json::from_str::<PythonRequirementManifest>(&seed))
        .transpose()
        .map_err(|error| format!("invalid managed Python seed: {error}"))?
        .map(PythonRequirementManifest::normalized);
    STATE.with_borrow_mut(|state| state.accepted = accepted);
    Ok(())
}

fn current() -> Result<PythonRequirementManifest, String> {
    STATE
        .with_borrow(|state| state.declared.as_ref().or(state.accepted.as_ref()).cloned())
        .ok_or_else(|| "Python preparation requires a server-managed interpreter".to_string())
}

pub(super) fn state() -> Value {
    STATE.with_borrow(|state| {
        json!({
            "manifest": state.declared.as_ref().or(state.accepted.as_ref()),
            "active": state.active,
        })
    })
}

#[derive(Deserialize)]
pub(super) struct Declaration {
    packages: Option<Vec<String>>,
    python_version: Option<Vec<String>>,
    exclude_newer: Option<String>,
    exclude_newer_supplied: bool,
    action: Action,
}

#[derive(Clone, Copy, Deserialize)]
#[serde(rename_all = "lowercase")]
enum Action {
    Add,
    Remove,
    Set,
}

fn change(current: &mut Vec<String>, values: Vec<String>, action: Action) {
    match action {
        Action::Add => current.extend(values),
        Action::Remove => current.retain(|value| !values.contains(value)),
        Action::Set => *current = values,
    }
}

pub(super) fn declare(request: Declaration) -> Result<(), String> {
    let mut candidate = current()?;
    let active = STATE.with_borrow(|state| state.active.clone());
    if let Some(active) = active {
        if let Some(versions) = request.python_version {
            crate::python_requirement::validate_version_constraints(&versions)?;
            let version = active["version"]
                .as_str()
                .ok_or("Python state omitted version")?;
            for clause in versions.iter().flat_map(|constraint| constraint.split(',')) {
                use pep508_rs::pep440_rs::{Version, VersionSpecifier};
                let clause = clause.trim();
                let live = version
                    .parse::<Version>()
                    .map_err(|error| error.to_string())?;
                let matches = if let Ok(required) = clause.parse::<Version>() {
                    live.release().starts_with(required.release())
                } else {
                    clause
                        .parse::<VersionSpecifier>()
                        .map_err(|error| error.to_string())?
                        .contains(&live)
                };
                if !matches {
                    return Err(format!(
                        "Python version requirements cannot be changed after Python has been initialized.\n* Python version request: '{}'\n* Python version initialized: '{version}'",
                        versions.join(",")
                    ));
                }
            }
        }
        if request.exclude_newer_supplied && request.exclude_newer != candidate.exclude_newer {
            return Err(
                "`exclude_newer` cannot be changed after Python has initialized.".to_string(),
            );
        }
        if let Some(packages) = request.packages {
            change(&mut candidate.packages, packages, request.action);
        }
        candidate = candidate.normalized();
        let previous = current()?.packages;
        if !previous
            .iter()
            .all(|package| candidate.packages.contains(package))
            || (matches!(request.action, Action::Set) && previous != candidate.packages)
        {
            return Err(
                "After Python has initialized, only `action = 'add'` is supported.".to_string(),
            );
        }
        prepare_candidate(candidate, None)
    } else {
        if let Some(packages) = request.packages {
            change(&mut candidate.packages, packages, request.action);
        }
        if let Some(versions) = request.python_version {
            change(&mut candidate.python_version, versions, request.action);
        }
        if request.exclude_newer_supplied {
            candidate.exclude_newer = match request.action {
                Action::Add if candidate.exclude_newer.is_some() => {
                    return Err(format!(
                        "`exclude_newer` is already set to '{}', use `action = 'set'` to override",
                        candidate.exclude_newer.unwrap()
                    ));
                }
                Action::Remove
                    if request.exclude_newer.is_none()
                        || request.exclude_newer == candidate.exclude_newer =>
                {
                    None
                }
                Action::Remove => candidate.exclude_newer,
                Action::Add | Action::Set => request.exclude_newer,
            };
        }
        STATE.with_borrow_mut(|state| state.declared = Some(candidate.normalized()));
        Ok(())
    }
}

fn resolve(
    candidate: PythonRequirementManifest,
    versions: Vec<String>,
    import_resolution: Option<PythonImportResolution>,
) -> Result<String, String> {
    crate::python_requirement::validate_all(&candidate.packages)?;
    crate::python_requirement::validate_version_constraints(&candidate.python_version)?;
    let mut requirements = candidate.clone();
    requirements.python_version = versions;
    let python = worker::resolve_python(PythonResolveRequest {
        requirements,
        retained_requirements: candidate.clone(),
        import_resolution,
    })?;
    STATE.with_borrow_mut(|state| state.provisional = Some(candidate));
    Ok(python)
}

/// Reticulate still chooses the initial interpreter. Its managed bootstrap asks
/// this owner for the candidate; a resolver pin never becomes a declaration.
pub(super) fn bootstrap(versions: Vec<String>) -> Result<String, String> {
    resolve(current()?, versions, None)
}

#[derive(Deserialize)]
pub(super) struct Preparation {
    pub(super) packages: Vec<String>,
    pub(super) import_resolution: Option<PythonImportResolution>,
}

pub(super) fn prepare(request: Preparation) -> Result<PreparationOutcome, String> {
    let mut result = current().and_then(|mut candidate| {
        candidate.packages.extend(request.packages);
        prepare_candidate(candidate.normalized(), request.import_resolution)
    });
    STATE.with_borrow_mut(|state| state.provisional = None);
    if worker::acknowledge_python_interrupt() {
        result = Err("KeyboardInterrupt".to_string());
    }
    Ok(match result {
        Ok(()) => PreparationOutcome::Prepared,
        Err(message) => PreparationOutcome::Failed { message },
    })
}

fn prepare_candidate(
    candidate: PythonRequirementManifest,
    import_resolution: Option<PythonImportResolution>,
) -> Result<(), String> {
    let (accepted, active) =
        STATE.with_borrow(|state| (state.accepted.clone(), state.active.clone()));
    if active.is_some() && accepted.as_ref() == Some(&candidate) {
        return Ok(());
    }
    let mut versions = candidate.python_version.clone();
    if let Some(active) = &active {
        versions.push(
            active["version"]
                .as_str()
                .ok_or("Python state omitted version")?
                .to_string(),
        );
    }
    let executable = resolve(candidate.clone(), versions, import_resolution)?;
    if active.is_some() {
        let inspected = probe::inspect(&executable)?;
        let response = library::environment_call(
            c"prepare",
            &json!({"environment": inspected, "manifest": candidate}).to_string(),
        )
        .map_err(infrastructure)?;
        match serde_json::from_str::<PreparationOutcome>(&response).map_err(|error| {
            infrastructure(format!("invalid Python preparation response: {error}"))
        })? {
            PreparationOutcome::Prepared => Ok(()),
            PreparationOutcome::Failed { message } => Err(message),
        }
    } else {
        if worker::python_interrupt_pending() {
            return Err("KeyboardInterrupt".to_string());
        }
        worker::begin_python_commit();
        let result = commit(candidate, None);
        let interrupted = worker::finish_python_commit();
        result?;
        if interrupted {
            return Err("KeyboardInterrupt".to_string());
        }
        Ok(())
    }
}

#[derive(Deserialize, Serialize)]
pub(super) struct Activation {
    pub(super) manifest: PythonRequirementManifest,
    pub(super) environment: Value,
}

pub(super) fn commit(
    manifest: PythonRequirementManifest,
    active: Option<Value>,
) -> Result<(), String> {
    let expected = STATE.with_borrow(|state| {
        state.provisional.clone().or_else(|| {
            // Startup selection hints can use an already retained seed without
            // asking the managed bootstrap for another candidate.
            state
                .active
                .is_none()
                .then(|| state.accepted.clone())
                .flatten()
        })
    });
    if expected.as_ref() != Some(&manifest) {
        return Err(infrastructure(
            "Python activation does not match its provisional manifest".to_string(),
        ));
    }
    worker::publish_python_activation(manifest.clone())?;
    STATE.with_borrow_mut(|state| {
        state.accepted = Some(manifest);
        state.declared = None;
        state.provisional = None;
        if let Some(active) = active {
            state.active = Some(active);
        }
    });
    Ok(())
}

pub(super) fn attach(libpython: &str) -> Result<(), String> {
    if STATE.with_borrow(|state| state.active.is_some()) {
        return Ok(());
    }
    let managed = STATE.with_borrow(|state| state.accepted.is_some());
    if !managed {
        return Ok(());
    }
    library::install_environment().map_err(infrastructure)?;
    let response =
        library::environment_call(c"initialize", &json!({"libpython": libpython}).to_string())
            .map_err(infrastructure)?;
    let active: Value = serde_json::from_str(&response)
        .map_err(|error| infrastructure(format!("invalid Python startup state: {error}")))?;
    let manifest = current()?;
    worker::begin_python_commit();
    let result = commit(manifest, Some(active));
    let interrupted = worker::finish_python_commit();
    result?;
    if interrupted {
        return Err("Python initialization interrupted after activation".to_string());
    }
    Ok(())
}

pub(super) fn infrastructure(message: String) -> String {
    worker::record_worker_failure(message.clone());
    message
}
