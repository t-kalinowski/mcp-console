//! Native managed Python requirements and activation commits.
//!
//! This owner chooses declaration transitions and when to prepare a candidate.
//! The native owner checks the selected environment and activates it through
//! the retained CPython library. The R adapter supplies candidate configuration
//! and preserves presentation metadata and the activation commit boundary.

mod activation;
mod r;

pub(crate) use activation::{
    ActivationFailure, ActivationInput, activate_managed_environment, ensure_libpython_compatible,
};

use r::{Adapter, Declaration, Record, Value};

type RResult<T> = std::result::Result<T, r::Error>;

// Character payloads retain NA and the original bytes. Decoding to UTF-8 here
// would change byte-marked or native-encoded R strings. Encoding tags and vector
// attributes belong to the adapter, alongside field presence/order and history.
type Characters = Vec<Option<Vec<u8>>>;

#[derive(Clone, Default)]
struct Manifest {
    packages: Option<Characters>,
    python_version: Option<Characters>,
    exclude_newer: Option<Characters>,
}

#[derive(Default)]
struct Requirements {
    // The live projection is worker state, not server acceptance. The exact R
    // representation below also retains provisional (unmaterialized) values.
    live: Option<crate::worker_protocol::NativePythonActivation>,
    resolved: Option<crate::worker_protocol::NativePythonActivation>,
    current: Option<Manifest>,
    // A transient matching key for an environment already activated by
    // Console, not a second independently mutable requirement manifest.
    pending_activation: Option<Manifest>,
}

thread_local! {
    static STATE: std::cell::RefCell<Requirements> = std::cell::RefCell::new(Requirements::default());
}

static INITIAL_MANIFEST: std::sync::OnceLock<Option<PythonRequirementManifest>> =
    std::sync::OnceLock::new();

pub(super) fn configure() -> Result<(), String> {
    let manifest = std::env::var("MCP_CONSOLE_MANAGED_PYTHON")
        .ok()
        .map(|value| {
            serde_json::from_str(&value)
                .map_err(|error| format!("invalid managed Python declaration: {error}"))
        })
        .transpose()?;
    INITIAL_MANIFEST
        .set(manifest)
        .map_err(|_| "Python requirements already configured".into())
}

impl Requirements {
    fn activation_pending(&self) -> bool {
        self.pending_activation.is_some()
    }

    fn check_activation(&self) -> std::result::Result<(), &'static str> {
        if self.activation_pending() {
            return Err("Python activation is awaiting a requirement update");
        }
        Ok(())
    }

    fn commit(&mut self, value: Manifest) -> bool {
        self.current = Some(value);
        self.pending_activation.take().is_some()
    }
}

#[derive(Clone, Copy)]
enum Action {
    Add,
    Remove,
    Set,
}

impl Requirements {
    // Candidates are detached projections of the existing store. Only the
    // active-binding write commits them, after reticulate updates its config.
    fn transition(
        adapter: &Adapter,
        mut candidate: Record,
        request: Declaration,
        initialized: bool,
    ) -> RResult<(Record, Value)> {
        let current_packages = candidate.get("packages")?;
        let current_cutoff = candidate.get("exclude_newer")?;
        let mut activate = false;
        if !initialized {
            for (field, requested) in [
                ("packages", &request.packages),
                ("python_version", &request.python_version),
            ] {
                if !requested.is_null() {
                    let current = candidate.get(field)?;
                    let value = match request.action {
                        Action::Add => current.union(requested)?,
                        Action::Remove => current.difference(requested)?,
                        Action::Set => requested.copy()?,
                    };
                    candidate.set(field, value)?;
                }
            }
            if request.exclude_newer_supplied {
                let cutoff = match request.action {
                    Action::Add => {
                        if !current_cutoff.is_null() {
                            return Err(format!(
                                "`exclude_newer` is already set to '{}', use `action = 'set'` to override",
                                current_cutoff.text()?
                            ).into());
                        }
                        request.exclude_newer.copy()?
                    }
                    Action::Remove => {
                        if request.exclude_newer.is_null()
                            || request.exclude_newer.identical(&current_cutoff)?
                        {
                            Value::null()
                        } else {
                            current_cutoff
                        }
                    }
                    Action::Set => request.exclude_newer.copy()?,
                };
                candidate.set("exclude_newer", cutoff)?;
            }
        } else {
            if !request.python_version.is_null() {
                adapter.call("check_version", &[request.record.value()])?;
            }
            if request.exclude_newer_supplied
                && !request.exclude_newer.identical(&current_cutoff)?
            {
                return Err(
                    "`exclude_newer` cannot be changed after Python has initialized.".into(),
                );
            }
            if !request.packages.is_null() {
                match request.action {
                    Action::Add => {
                        let added = request.packages.difference(&current_packages)?;
                        if !added.is_empty() {
                            adapter.call("check_packages", &[&added, &current_packages])?;
                            candidate.set("packages", added.union(&current_packages)?)?;
                            activate = true;
                        }
                    }
                    Action::Remove | Action::Set => {
                        let unchanged = match request.action {
                            Action::Remove => request.packages.disjoint(&current_packages)?,
                            Action::Set => request.packages.set_equal(&current_packages)?,
                            Action::Add => unreachable!(),
                        };
                        if !unchanged {
                            return Err(
                                "After Python has initialized, only `action = 'add'` is supported."
                                    .into(),
                            );
                        }
                    }
                }
            }
        }
        let config = if activate {
            // No state borrow survives a compatibility check, resolver, or
            // activation callback. The interpreter pin is resolver input only.
            r::check_activation().map_err(r::from_r_error)?;
            let version = adapter.call("live_python_version", &[])?;
            let python = adapter.resolve(&candidate, &version)?;
            adapter.activate(&python, &candidate)?
        } else {
            Value::null()
        };
        candidate.append_history(&request.record)?;
        Ok((candidate, config))
    }
}

use super::NativePython;
use crate::worker_protocol::{
    NativePythonActivation, PythonImportResolution, PythonRequirementManifest, PythonResolveRequest,
};

pub(crate) enum ActivationOutcome {
    Prepared,
    Rejected(String),
    Failed(String),
}

pub(super) fn declaration() -> Result<PythonRequirementManifest, String> {
    if let Some(manifest) = r::declaration()? {
        return Ok(manifest);
    }
    retained_manifest()
        .ok_or_else(|| "Python preparation requires a server-managed interpreter".into())
}

pub(super) fn prepare(packages: Vec<String>) -> Result<super::PreparationOutcome, String> {
    let mut requirements = declaration()?;
    requirements.packages.extend(packages.iter().cloned());
    let requirements = requirements.normalized();
    let live = super::library::initialized_selection()?.is_some();
    let candidate = match crate::worker::resolve_python(PythonResolveRequest {
        requirements: requirements.clone(),
        retained_requirements: requirements,
        initialized: live,
        import_resolution: None,
    }) {
        Ok(candidate) => candidate,
        Err(message) => return Ok(super::PreparationOutcome::Rejected { message }),
    };
    if live && let Err(error) = validate_selected(&candidate.selected) {
        return Ok(super::PreparationOutcome::Rejected {
            message: error.to_string(),
        });
    }
    let projection = match r::project_packages(&candidate.selected, &packages) {
        Ok(projection) => projection,
        Err(message) => return Ok(super::PreparationOutcome::Rejected { message }),
    };
    resolved(candidate.clone());
    if live {
        match activate(&candidate)? {
            ActivationOutcome::Prepared => {}
            ActivationOutcome::Rejected(message) => {
                return Ok(super::PreparationOutcome::Rejected { message });
            }
            ActivationOutcome::Failed(message) => {
                crate::worker::publish_python_activation_failure(candidate.requirements)?;
                return Ok(super::PreparationOutcome::Failed { message });
            }
        }
    }
    if let Some(projection) = projection {
        if let Err(message) = projection.commit() {
            if live {
                crate::worker::publish_python_activation_failure(candidate.requirements)?;
                return Ok(super::PreparationOutcome::Failed {
                    message: format!("{message}; restart required"),
                });
            }
            return Ok(super::PreparationOutcome::Rejected { message });
        }
    } else if live {
        crate::worker::publish_python_activation(candidate.requirements)?;
    }
    Ok(super::PreparationOutcome::Prepared)
}

pub(super) fn retained_manifest() -> Option<PythonRequirementManifest> {
    STATE
        .with(|slot| {
            let state = slot.borrow();
            state
                .live
                .as_ref()
                .or(state.resolved.as_ref())
                .map(|candidate| candidate.requirements.clone())
        })
        .or_else(|| INITIAL_MANIFEST.get().cloned().flatten())
}

pub(super) fn materialized() -> Option<NativePythonActivation> {
    STATE.with(|slot| slot.borrow().resolved.clone())
}

pub(super) fn initialized() -> bool {
    STATE.with(|slot| slot.borrow().live.is_some())
}

pub(crate) fn initialize(
    selected: &NativePython,
    requirements: PythonRequirementManifest,
) -> Result<(), String> {
    let requirements = requirements.normalized();
    STATE.with(|slot| {
        let mut state = slot.borrow_mut();
        if state.live.is_some() {
            return Err("managed Python state is already initialized".to_string());
        }
        state.live = Some(NativePythonActivation {
            selected: selected.clone(),
            requirements: requirements.clone(),
        });
        Ok(())
    })?;
    // Runtime initialization is demand-driven, always after worker readiness.
    crate::worker::publish_python_activation(requirements)
}

fn snapshot() -> Result<NativePythonActivation, String> {
    STATE
        .with(|state| state.borrow().live.clone())
        .ok_or_else(|| "managed Python state is unavailable".into())
}

pub(super) fn resolved(candidate: NativePythonActivation) {
    STATE.with(|state| state.borrow_mut().resolved = Some(candidate));
}

pub(super) fn resolved_selection() -> Option<NativePython> {
    STATE.with(|state| {
        state
            .borrow()
            .resolved
            .as_ref()
            .map(|value| value.selected.clone())
    })
}

pub(super) fn activation_failed() -> Result<(), String> {
    let candidate = STATE
        .with(|state| state.borrow().resolved.clone())
        .ok_or("Python activation has no resolved candidate")?;
    crate::worker::publish_python_activation_failure(candidate.requirements)
}

pub(super) fn validate_selected(candidate: &NativePython) -> Result<(), ActivationFailure> {
    let running =
        super::library::selected_configuration().map_err(ActivationFailure::BeforeMutation)?;
    ensure_libpython_compatible(&candidate.embedding.libpython, &running.embedding.libpython)
}

pub(super) fn accept(requirements: PythonRequirementManifest) -> Result<(), String> {
    let requirements = requirements.normalized();
    STATE.with(|slot| {
        let mut state = slot.borrow_mut();
        let candidate = state
            .resolved
            .as_ref()
            .filter(|candidate| candidate.requirements == requirements)
            .or_else(|| {
                state
                    .live
                    .as_ref()
                    .filter(|candidate| candidate.requirements == requirements)
            })
            .ok_or("Python activation has no matching inspected candidate")?
            .clone();
        super::library::accept_configuration(&candidate.selected)?;
        state.live = Some(candidate);
        Ok(())
    })
}

pub(crate) fn activate(candidate: &NativePythonActivation) -> Result<ActivationOutcome, String> {
    match activate_selected(&candidate.selected) {
        Ok(()) => {
            super::library::accept_configuration(&candidate.selected)?;
            STATE.with(|state| state.borrow_mut().live = Some(candidate.clone()));
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

/// Mutate only the live interpreter. The caller commits its declaration and
/// reports activation afterwards, through its own condition/metadata adapter.
pub(super) fn activate_selected(candidate: &NativePython) -> Result<(), ActivationFailure> {
    let running =
        super::library::selected_configuration().map_err(ActivationFailure::BeforeMutation)?;
    activate_managed_environment(ActivationInput {
        candidate_python: &candidate.embedding.python,
        candidate_libpython: &candidate.embedding.libpython,
        candidate_executable: &candidate.embedding.python,
        running_libpython: &running.embedding.libpython,
    })?;
    match super::library::configure_native_child_environment(candidate) {
        Ok(true) => Ok(()),
        Ok(false) => Err(ActivationFailure::PythonException),
        Err(error) => Err(ActivationFailure::Infrastructure(error)),
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
        initialized: true,
        import_resolution: Some(resolution.clone()),
    };
    let candidate = match crate::worker::resolve_python(request) {
        Ok(candidate) => candidate,
        Err(error) => return Ok(failed(error)),
    };
    if candidate.requirements != requirements {
        return Err("native Python resolver returned an unexpected declaration".into());
    }
    if let Err(error) = validate_selected(&candidate.selected) {
        return Ok(failed(error.to_string()));
    }
    let projection = match r::project_packages(
        &candidate.selected,
        std::slice::from_ref(&resolution.distribution),
    ) {
        Ok(projection) => projection,
        Err(error) => return Ok(failed(error)),
    };
    resolved(candidate.clone());
    match activate(&candidate)? {
        ActivationOutcome::Prepared => {
            if let Some(projection) = projection {
                if let Err(error) = projection.commit() {
                    crate::worker::publish_python_activation_failure(candidate.requirements)?;
                    return Ok(failed(format!(
                        "{error}; Python activation failed; restart required"
                    )));
                }
            } else {
                crate::worker::publish_python_activation(candidate.requirements)?;
            }
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
