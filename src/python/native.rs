//! Worker copy of the accepted local managed Python selection.
//! The server remains the authority for preparation and retained requirements.

use std::sync::{Mutex, OnceLock};

use crate::worker_protocol::{
    NativePythonActivation, PythonImportResolution, PythonRequirementManifest, PythonResolveRequest,
};

use super::{ActivationFailure, ActivationInput, NativePython};

#[derive(Clone)]
struct State {
    selected: NativePython,
    requirements: PythonRequirementManifest,
}

static STATE: OnceLock<Mutex<State>> = OnceLock::new();

pub(crate) enum ActivationOutcome {
    Prepared,
    Rejected(String),
    Failed(String),
}

pub(crate) fn initialize(
    selected: &NativePython,
    requirements: PythonRequirementManifest,
) -> Result<(), String> {
    STATE
        .set(Mutex::new(State {
            selected: selected.clone(),
            requirements: requirements.normalized(),
        }))
        .map_err(|_| "native managed Python state is already initialized".to_string())
}

fn snapshot() -> Result<State, String> {
    STATE
        .get()
        .ok_or("native managed Python state is unavailable")?
        .lock()
        .map_err(|_| "native managed Python state lock poisoned".to_string())
        .map(|state| state.clone())
}

pub(crate) fn activate(candidate: &NativePythonActivation) -> Result<ActivationOutcome, String> {
    let running = snapshot()?;
    let input = ActivationInput {
        candidate_python: &candidate.selected.embedding.python,
        candidate_libpython: &candidate.selected.embedding.libpython,
        candidate_executable: &candidate.selected.embedding.python,
        running_libpython: &running.selected.embedding.libpython,
    };
    let result = super::activate_managed_environment(input).and_then(|()| {
        match super::library::configure_native_child_environment(&candidate.selected) {
            Ok(true) => Ok(()),
            Ok(false) => Err(ActivationFailure::PythonException),
            Err(error) => Err(ActivationFailure::Infrastructure(error)),
        }
    });
    match result {
        Ok(()) => {
            let mut state = STATE
                .get()
                .expect("native managed Python was initialized")
                .lock()
                .map_err(|_| "native managed Python state lock poisoned".to_string())?;
            state.selected = candidate.selected.clone();
            state.requirements = candidate.requirements.clone();
            Ok(ActivationOutcome::Prepared)
        }
        Err(
            error @ (ActivationFailure::Incompatible { .. } | ActivationFailure::BeforeMutation(_)),
        ) => Ok(ActivationOutcome::Rejected(error.to_string())),
        Err(ActivationFailure::PythonException) => {
            super::library::display_activation_exception()?;
            Ok(ActivationOutcome::Failed(
                "Python activation failed; restart required".into(),
            ))
        }
        Err(error) => Ok(ActivationOutcome::Failed(format!(
            "{error}; Python activation failed; restart required"
        ))),
    }
}

pub(crate) fn resolve_import(resolution: PythonImportResolution) -> Result<String, String> {
    let current = snapshot()?;
    if current
        .requirements
        .packages
        .contains(&resolution.distribution)
    {
        return Ok(ready());
    }
    let mut requirements = current.requirements;
    requirements.packages.push(resolution.distribution.clone());
    let requirements = requirements.normalized();
    let request = PythonResolveRequest {
        requirements: requirements.clone(),
        retained_requirements: requirements.clone(),
        import_resolution: Some(resolution),
    };
    let candidate = match crate::worker::resolve_native_python(request) {
        Ok(candidate) => candidate,
        Err(error) => return Ok(failed(error)),
    };
    if candidate.requirements != requirements {
        return Err("native Python resolver returned an unexpected declaration".into());
    }
    match activate(&candidate)? {
        ActivationOutcome::Prepared => {
            crate::worker::publish_python_activation(candidate.requirements)?;
            Ok(ready())
        }
        ActivationOutcome::Rejected(error) => Ok(failed(error)),
        ActivationOutcome::Failed(error) => {
            crate::worker::publish_python_activation_failure(candidate.requirements)?;
            Ok(failed(error))
        }
    }
}

fn ready() -> String {
    r#"{"kind":"ready"}"#.into()
}

fn failed(message: String) -> String {
    serde_json::json!({ "kind": "failed", "message": message }).to_string()
}
