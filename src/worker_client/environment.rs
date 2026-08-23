use std::collections::BTreeSet;
use std::ffi::OsString;

use super::lifecycle::{
    FailedWorkerStop, GenerationStatus, LifecycleState, OldGenerationCommitDisposition,
    RequirementChangeState, WorkerGeneration,
};
use super::{Client, EnvironmentPreparationAdmissionFailure, PreparationOutcome, WorkerState};

pub(super) struct Environment {
    pub(super) custom_worker: bool,
    pub(super) duckdb_extensions: BTreeSet<String>,
    /// R libraries that may have supplied DuckDB in the current worker generation.
    pub(super) duckdb_r_targets: Vec<crate::resolver::ManagedR>,
    pub(super) python: Option<PythonEnvironment>,
    pub(super) r: Option<crate::resolver::ManagedR>,
}

const USER_SELECTED_PYTHON_ERROR: &str = "managed Python requirements are disabled because the session uses a user-selected Python environment";

#[derive(Clone)]
pub(super) enum PythonEnvironment {
    Managed {
        selected: crate::resolver::ManagedPython,
        resolver: crate::resolver::ManagedPythonResolverConfiguration,
    },
    UserSelected(OsString),
}

impl PythonEnvironment {
    pub(super) fn builtin(
        configured: Option<OsString>,
        resolver: crate::resolver::ManagedPythonResolverConfiguration,
        managed_r: Option<&crate::resolver::ManagedR>,
    ) -> Result<Self, String> {
        if let Some(configured) = configured
            && !configured.is_empty()
            && configured != "managed"
        {
            return Ok(Self::UserSelected(configured));
        }
        let selected = crate::resolver::resolve_python(&[], &resolver, managed_r, |_| Ok(()))?;
        Ok(Self::Managed { selected, resolver })
    }

    pub(super) fn managed(&self) -> Option<&crate::resolver::ManagedPython> {
        match self {
            Self::Managed { selected, .. } => Some(selected),
            Self::UserSelected(_) => None,
        }
    }

    pub(super) fn managed_parts(
        &self,
    ) -> Result<
        (
            &crate::resolver::ManagedPython,
            &crate::resolver::ManagedPythonResolverConfiguration,
        ),
        String,
    > {
        match self {
            Self::Managed { selected, resolver } => Ok((selected, resolver)),
            Self::UserSelected(_) => Err(USER_SELECTED_PYTHON_ERROR.to_string()),
        }
    }

    pub(super) fn replace_managed(
        &mut self,
        managed: crate::resolver::ManagedPython,
    ) -> Result<(), String> {
        match self {
            Self::Managed { selected, .. } => {
                *selected = managed;
                Ok(())
            }
            Self::UserSelected(_) => Err(USER_SELECTED_PYTHON_ERROR.to_string()),
        }
    }

    #[cfg(target_os = "macos")]
    pub(super) fn configure_worker(&self, command: &mut crate::sandbox::SandboxedCommand) {
        match self {
            Self::Managed { selected, .. } => selected.configure_worker(command),
            Self::UserSelected(python) => {
                command
                    .env("RETICULATE_PYTHON", python)
                    .env_remove("MCP_CONSOLE_MANAGED_PYTHON");
            }
        }
    }
}

pub(super) fn ensure_python_additions_available(
    environment: &Environment,
    additions: &[String],
) -> Result<(), String> {
    if additions.is_empty() {
        return Ok(());
    }
    if environment.custom_worker {
        return Err("Python requirements are unavailable with a custom worker".to_string());
    }
    match environment.python.as_ref() {
        Some(PythonEnvironment::Managed { .. }) => Ok(()),
        Some(PythonEnvironment::UserSelected(_)) => Err(USER_SELECTED_PYTHON_ERROR.to_string()),
        None => Err("managed Python environment is unavailable".to_string()),
    }
}

pub(crate) enum PrepareResult {
    Prepared,
    RestartRequired,
    Failed(super::Response),
    WorkerStopped(super::Response),
}

#[derive(Clone, Copy)]
pub(super) enum PreparationIntent {
    Standalone,
    BeforeEvaluation,
}

pub(crate) struct Requirements {
    pub(crate) duckdb: Vec<String>,
    pub(crate) python: Vec<String>,
    pub(crate) r: Vec<String>,
}

pub(super) struct RequirementDelta {
    duckdb_extensions: BTreeSet<String>,
    duckdb_changed: bool,
    python_additions: BTreeSet<String>,
    python_candidate: Option<crate::worker_protocol::PythonRequirementManifest>,
    r_requirements: Vec<String>,
    r_changed: bool,
}

impl RequirementDelta {
    pub(super) fn calculate(
        environment: &Environment,
        requirements: Requirements,
    ) -> Result<Self, String> {
        let Requirements { duckdb, python, r } = requirements;
        ensure_python_additions_available(environment, &python)?;

        let duckdb_additions = duckdb.into_iter().collect::<BTreeSet<_>>();
        let duckdb_changed = !duckdb_additions.is_subset(&environment.duckdb_extensions);
        let duckdb_extensions = environment
            .duckdb_extensions
            .union(&duckdb_additions)
            .cloned()
            .collect();

        let python_additions = python.into_iter().collect::<BTreeSet<_>>();
        let python_candidate = merge_python_requirements(
            environment
                .python
                .as_ref()
                .and_then(PythonEnvironment::managed),
            python_additions.iter().cloned().collect(),
        );

        let (r_requirements, r_changed) = merge_r_requirements(environment, r);

        Ok(Self {
            duckdb_extensions,
            duckdb_changed,
            python_additions,
            python_candidate,
            r_requirements,
            r_changed,
        })
    }

    pub(super) fn is_empty(&self) -> bool {
        !self.duckdb_changed && self.python_candidate.is_none() && !self.r_changed
    }
}

pub(super) struct ResolvedEnvironment {
    pub(super) duckdb_extensions: BTreeSet<String>,
    pub(super) managed_python: Option<crate::resolver::ManagedPython>,
    pub(super) managed_r: Option<crate::resolver::ManagedR>,
}

pub(super) enum EnvironmentResolutionFailure {
    Host(String),
    Interrupted(String),
    Cancelled(String),
    Operation(String),
}

impl EnvironmentResolutionFailure {
    pub(super) fn into_message(self) -> String {
        match self {
            Self::Host(message)
            | Self::Interrupted(message)
            | Self::Cancelled(message)
            | Self::Operation(message) => message,
        }
    }
}

pub(super) enum RuntimeRResolutionFailure {
    Ordinary(String),
    Interrupted,
    Cancelled(String),
    Infrastructure(String),
}

impl From<EnvironmentResolutionFailure> for RuntimeRResolutionFailure {
    fn from(failure: EnvironmentResolutionFailure) -> Self {
        match failure {
            EnvironmentResolutionFailure::Host(message) => Self::Ordinary(message),
            EnvironmentResolutionFailure::Interrupted(_) => Self::Interrupted,
            EnvironmentResolutionFailure::Cancelled(message) => Self::Cancelled(message),
            EnvironmentResolutionFailure::Operation(message) => Self::Infrastructure(message),
        }
    }
}

fn merge_r_requirements(environment: &Environment, additions: Vec<String>) -> (Vec<String>, bool) {
    let mut additions = additions.into_iter().collect::<BTreeSet<_>>();
    if environment.custom_worker {
        additions.extend(
            super::CUSTOM_DUCKDB_R_REQUIREMENTS
                .iter()
                .map(|requirement| (*requirement).to_string()),
        );
    }
    let current = environment
        .r
        .as_ref()
        .map(|managed| managed.requirements().iter().cloned().collect())
        .unwrap_or_default();
    let changed = !additions.is_subset(&current);
    (current.union(&additions).cloned().collect(), changed)
}

pub(super) fn merge_python_requirements(
    current: Option<&crate::resolver::ManagedPython>,
    additions: Vec<String>,
) -> Option<crate::worker_protocol::PythonRequirementManifest> {
    let retained = current
        .map(|managed| managed.requirements().packages.iter().cloned().collect())
        .unwrap_or_default();
    let mut candidate = current
        .map(|managed| managed.requirements().clone())
        .unwrap_or_else(crate::worker_protocol::default_python_requirement_manifest);
    let additions = additions.into_iter().collect::<BTreeSet<_>>();
    if additions.is_subset(&retained) {
        return None;
    }
    candidate.packages.extend(additions);
    Some(candidate.normalized())
}

fn select_python_activation(
    current: Option<&crate::resolver::ManagedPython>,
    requirements: crate::worker_protocol::PythonRequirementManifest,
    candidates: &mut Vec<crate::resolver::ManagedPython>,
) -> Result<crate::resolver::ManagedPython, String> {
    let requirements = requirements.normalized();
    if let Some(index) = candidates
        .iter()
        .rposition(|candidate| candidate.requirements() == &requirements)
    {
        return Ok(candidates.remove(index));
    }
    current
        .cloned()
        .filter(|current| current.requirements() == &requirements)
        .ok_or_else(|| "worker activation does not match a resolved Python environment".to_string())
}

fn select_r_activation(
    library: &str,
    candidates: &mut Vec<crate::resolver::ManagedR>,
) -> Result<crate::resolver::ManagedR, String> {
    let candidate = candidates
        .iter()
        .rposition(|candidate| candidate.library().to_str() == Some(library))
        .map(|index| candidates.remove(index));
    candidates.clear();
    candidate.ok_or_else(|| "worker activation does not match a resolved R environment".to_string())
}

fn push_duckdb_r_target(
    targets: &mut Vec<crate::resolver::ManagedR>,
    candidate: crate::resolver::ManagedR,
) {
    if targets
        .iter()
        .all(|target| target.library() != candidate.library())
    {
        targets.push(candidate);
    }
}

fn commit_managed_r(environment: &mut Environment, managed_r: crate::resolver::ManagedR) {
    push_duckdb_r_target(&mut environment.duckdb_r_targets, managed_r.clone());
    environment.r = Some(managed_r);
}

fn classify_resolver_result<T>(
    result: Result<T, String>,
    handle: Option<&crate::resolver::ResolverStopHandle>,
) -> Result<T, EnvironmentResolutionFailure> {
    result.map_err(
        |message| match handle.and_then(|handle| handle.control_outcome()) {
            Some(crate::resolver::ResolverControlOutcome::Interrupted) => {
                EnvironmentResolutionFailure::Interrupted(message)
            }
            Some(crate::resolver::ResolverControlOutcome::Cancelled) => {
                EnvironmentResolutionFailure::Cancelled(message)
            }
            None => EnvironmentResolutionFailure::Host(message),
        },
    )
}

impl Client {
    /// Adds requirements to the managed environment.
    pub(crate) async fn prepare(
        &self,
        requirements: Requirements,
    ) -> Result<PrepareResult, String> {
        let client = self.clone();
        tokio::task::spawn_blocking(move || client.prepare_blocking(requirements))
            .await
            .map_err(|error| format!("requirement preparation task failed: {error}"))?
    }

    fn prepare_blocking(&self, requirements: Requirements) -> Result<PrepareResult, String> {
        let generation = self.admit()?;
        let preparation = self.admit_preparation()?;
        self.prepare_admitted(
            requirements,
            &generation,
            &preparation,
            PreparationIntent::Standalone,
        )
    }

    pub(super) fn prepare_admitted(
        &self,
        requirements: Requirements,
        generation: &WorkerGeneration,
        _preparation: &tokio::sync::RwLockWriteGuard<'_, ()>,
        intent: PreparationIntent,
    ) -> Result<PrepareResult, String> {
        let environment = self
            .0
            .environment
            .as_ref()
            .ok_or_else(|| "managed requirements are unavailable".to_string())?;
        let active_operation = self
            .evaluation()?
            .as_ref()
            .map(|active| active.evaluation.clone());
        if let Some(active) = active_operation {
            let environment = match environment.try_lock() {
                Ok(environment) => environment,
                Err(std::sync::TryLockError::WouldBlock) => {
                    return Err(active.reject_preparation_message().to_string());
                }
                Err(std::sync::TryLockError::Poisoned(_)) => {
                    return Err("worker environment lock poisoned".to_string());
                }
            };
            self.ensure_generation(generation)?;
            let delta = RequirementDelta::calculate(&environment, requirements)?;
            if delta.is_empty() {
                return Ok(PrepareResult::Prepared);
            }
            if matches!(intent, PreparationIntent::Standalone)
                && self.requirement_change_state(generation)?
                    == RequirementChangeState::RestartRequired
            {
                return Ok(PrepareResult::RestartRequired);
            }
            return Err(active.reject_preparation_message().to_string());
        }
        let mut pending_requirements = Some(requirements);
        let available_environment = match environment.try_lock() {
            Ok(environment) => {
                self.ensure_generation(generation)?;
                let delta = RequirementDelta::calculate(
                    &environment,
                    pending_requirements
                        .take()
                        .expect("environment requirements were already consumed"),
                )?;
                if delta.is_empty() {
                    return Ok(PrepareResult::Prepared);
                }
                Some((environment, delta))
            }
            Err(std::sync::TryLockError::WouldBlock) => None,
            Err(std::sync::TryLockError::Poisoned(_)) => {
                return Err("worker environment lock poisoned".to_string());
            }
        };
        let mut worker = match self.0.worker.try_lock() {
            Ok(worker) => worker,
            Err(std::sync::TryLockError::WouldBlock) => {
                return Err("[requirements not prepared: worker is starting]".to_string());
            }
            Err(std::sync::TryLockError::Poisoned(_)) => {
                return Err("worker lock poisoned".to_string());
            }
        };
        let environment_preparation = if let WorkerState::Running(running) = &*worker {
            match running.reserve_environment_preparation() {
                Ok(reservation) => Ok(Some(reservation)),
                Err(EnvironmentPreparationAdmissionFailure::Busy(error)) => {
                    return self.finish_environment_resolution_failure(
                        generation,
                        intent,
                        EnvironmentResolutionFailure::Host(error),
                    );
                }
                Err(EnvironmentPreparationAdmissionFailure::Infrastructure(error)) => Err(error),
            }
        } else {
            Ok(None)
        };
        let (mut environment, delta) = match available_environment {
            Some(snapshot) => snapshot,
            None => {
                let environment = environment
                    .lock()
                    .map_err(|_| "worker environment lock poisoned".to_string())?;
                self.ensure_generation(generation)?;
                let delta = RequirementDelta::calculate(
                    &environment,
                    pending_requirements
                        .take()
                        .expect("environment requirements were already consumed"),
                )?;
                if delta.is_empty() {
                    return Ok(PrepareResult::Prepared);
                }
                (environment, delta)
            }
        };
        let includes_r = delta.r_changed;
        let _environment_preparation = match environment_preparation {
            Ok(reservation) => reservation,
            Err(error) => {
                drop(environment);
                return self.fail_running_preparation(
                    &mut worker,
                    generation,
                    true,
                    error,
                    includes_r,
                );
            }
        };
        if self.requirement_change_state(generation)? == RequirementChangeState::RestartRequired {
            return Ok(PrepareResult::RestartRequired);
        }
        if matches!(*worker, WorkerState::Stopped)
            && matches!(intent, PreparationIntent::Standalone)
        {
            return Ok(PrepareResult::RestartRequired);
        }
        if matches!(*worker, WorkerState::Running(_)) {
            let RequirementDelta {
                duckdb_extensions,
                duckdb_changed,
                python_additions,
                python_candidate,
                r_requirements,
                r_changed,
            } = delta;
            let managed_r = if r_changed {
                match self.resolve_managed_r(generation, r_requirements) {
                    Ok(managed_r) => Some(managed_r),
                    Err(failure) => {
                        return self
                            .finish_environment_resolution_failure(generation, intent, failure);
                    }
                }
            } else {
                None
            };
            if !duckdb_extensions.is_empty() && (duckdb_changed || managed_r.is_some()) {
                let mut targets = Vec::new();
                if duckdb_changed {
                    targets.extend(environment.duckdb_r_targets.iter().cloned());
                }
                if let Some(managed_r) = managed_r.as_ref() {
                    push_duckdb_r_target(&mut targets, managed_r.clone());
                }
                let duckdb_extensions = duckdb_extensions.iter().cloned().collect::<Vec<_>>();
                if let Err(failure) =
                    self.resolve_duckdb_extensions(generation, &targets, &duckdb_extensions)
                {
                    return self.finish_environment_resolution_failure(generation, intent, failure);
                }
            }
            let python_packages = if python_candidate.is_some() {
                python_additions.into_iter().collect()
            } else {
                Vec::new()
            };
            let duckdb_candidate = duckdb_changed.then_some(duckdb_extensions);
            if python_packages.is_empty() && managed_r.is_none() {
                if let Some(duckdb_candidate) = duckdb_candidate {
                    self.commit_locked_duckdb_environment(
                        generation,
                        &mut environment,
                        duckdb_candidate,
                    )?;
                }
                return Ok(PrepareResult::Prepared);
            }
            drop(environment);
            return self.prepare_running(
                generation,
                worker,
                python_packages,
                managed_r,
                duckdb_candidate,
            );
        }

        let resolved = match self.resolve_prestart_environment(generation, &environment, delta) {
            Ok(resolved) => resolved,
            Err(failure) => {
                return self.finish_environment_resolution_failure(generation, intent, failure);
            }
        };

        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.generation.is(generation) => {
                lifecycle.processes.resolver = None;
                if let Some(managed_python) = resolved.managed_python {
                    environment
                        .python
                        .as_mut()
                        .ok_or_else(|| "managed Python environment is unavailable".to_string())?
                        .replace_managed(managed_python)?;
                }
                environment.r = resolved.managed_r;
                environment.duckdb_extensions = resolved.duckdb_extensions;
                Ok(PrepareResult::Prepared)
            }
            LifecycleState::Ready => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    pub(super) fn resolve_prestart_environment(
        &self,
        generation: &WorkerGeneration,
        environment: &Environment,
        delta: RequirementDelta,
    ) -> Result<ResolvedEnvironment, EnvironmentResolutionFailure> {
        let RequirementDelta {
            duckdb_extensions,
            duckdb_changed,
            python_additions: _,
            python_candidate,
            r_requirements,
            r_changed,
        } = delta;
        let mut managed_r = environment.r.clone();
        if r_changed {
            managed_r = Some(self.resolve_managed_r(generation, r_requirements)?);
        }
        if !duckdb_extensions.is_empty() && (duckdb_changed || r_changed) {
            let target = managed_r.as_ref().ok_or_else(|| {
                EnvironmentResolutionFailure::Operation(
                    "DuckDB extension preparation requires a managed R environment".to_string(),
                )
            })?;
            let extensions = duckdb_extensions.iter().cloned().collect::<Vec<_>>();
            self.resolve_duckdb_extensions(generation, std::slice::from_ref(target), &extensions)?;
        }
        let managed_python = if let Some(candidate) = python_candidate {
            let (_, resolver) = environment
                .python
                .as_ref()
                .ok_or_else(|| {
                    EnvironmentResolutionFailure::Operation(
                        "managed Python environment is unavailable".to_string(),
                    )
                })?
                .managed_parts()
                .map_err(EnvironmentResolutionFailure::Operation)?;
            Some(self.resolve_managed_python_host(
                generation,
                candidate,
                resolver,
                managed_r.as_ref(),
            )?)
        } else {
            None
        };
        Ok(ResolvedEnvironment {
            duckdb_extensions,
            managed_python,
            managed_r,
        })
    }

    fn resolve_managed_r(
        &self,
        generation: &WorkerGeneration,
        requirements: Vec<String>,
    ) -> Result<crate::resolver::ManagedR, EnvironmentResolutionFailure> {
        let mut stop_handle = None;
        let result = crate::resolver::resolve_r(requirements, |handle| {
            stop_handle = Some(handle.clone());
            self.register_resolver_stop_handle(generation, handle)
        });
        self.clear_resolver_stop_handle(generation)
            .map_err(EnvironmentResolutionFailure::Operation)?;
        classify_resolver_result(result, stop_handle.as_ref())
    }

    fn resolve_managed_python_host(
        &self,
        generation: &WorkerGeneration,
        requirements: crate::worker_protocol::PythonRequirementManifest,
        resolver: &crate::resolver::ManagedPythonResolverConfiguration,
        managed_r: Option<&crate::resolver::ManagedR>,
    ) -> Result<crate::resolver::ManagedPython, EnvironmentResolutionFailure> {
        let result =
            crate::resolver::resolve_python_host(requirements, resolver, managed_r, |handle| {
                self.register_resolver_stop_handle(generation, handle)
            });
        self.clear_resolver_stop_handle(generation)
            .map_err(EnvironmentResolutionFailure::Operation)?;
        result.map_err(EnvironmentResolutionFailure::Host)
    }

    fn resolve_duckdb_extensions(
        &self,
        generation: &WorkerGeneration,
        managed_r: &[crate::resolver::ManagedR],
        extensions: &[String],
    ) -> Result<(), EnvironmentResolutionFailure> {
        if managed_r.is_empty() {
            return Err(EnvironmentResolutionFailure::Operation(
                "DuckDB extension preparation requires a managed R environment".to_string(),
            ));
        }
        for managed_r in managed_r {
            let mut stop_handle = None;
            let result =
                crate::resolver::resolve_duckdb_extensions(managed_r, extensions, |handle| {
                    stop_handle = Some(handle.clone());
                    self.register_resolver_stop_handle(generation, handle)
                });
            self.clear_resolver_stop_handle(generation)
                .map_err(EnvironmentResolutionFailure::Operation)?;
            classify_resolver_result(result, stop_handle.as_ref())?;
        }
        Ok(())
    }

    fn finish_environment_resolution_failure(
        &self,
        generation: &WorkerGeneration,
        intent: PreparationIntent,
        failure: EnvironmentResolutionFailure,
    ) -> Result<PrepareResult, String> {
        match (intent, failure) {
            (
                PreparationIntent::BeforeEvaluation,
                EnvironmentResolutionFailure::Host(error)
                | EnvironmentResolutionFailure::Interrupted(error),
            ) => match self.generation_status(generation)? {
                GenerationStatus::CurrentReady => Ok(self.failed_pre_evaluation_resolution(error)),
                GenerationStatus::CurrentClosing | GenerationStatus::Changed => Err(error),
            },
            (_, failure) => Err(failure.into_message()),
        }
    }

    fn commit_locked_duckdb_environment(
        &self,
        generation: &WorkerGeneration,
        environment: &mut Environment,
        duckdb_extensions: BTreeSet<String>,
    ) -> Result<(), String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.generation.is(generation) => {
                environment.duckdb_extensions = duckdb_extensions;
                Ok(())
            }
            LifecycleState::Ready => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    fn prepare_running(
        &self,
        generation: &WorkerGeneration,
        mut worker: std::sync::MutexGuard<'_, WorkerState>,
        python_packages: Vec<String>,
        managed_r: Option<crate::resolver::ManagedR>,
        duckdb_extensions: Option<BTreeSet<String>>,
    ) -> Result<PrepareResult, String> {
        self.ensure_generation(generation)?;
        let WorkerState::Running(running) = &mut *worker else {
            return Err("worker state changed during requirement preparation".to_string());
        };
        let includes_r = managed_r.is_some();
        let includes_python = !python_packages.is_empty();
        if includes_python {
            // The live worker has not accepted `managed_r` yet. Its Python
            // activation commits independently if that later R update fails,
            // so nested resolution uses the retained R environment.
            let client = self.clone();
            let commit_generation = generation.clone();
            let python_duckdb = if includes_r {
                None
            } else {
                duckdb_extensions.clone()
            };
            let commit = Box::new(move |result| {
                let managed = match result {
                    Ok(managed) => managed,
                    Err(error) => return Ok(PreparationOutcome::Completed(Err(error))),
                };
                if client.old_generation_commit_disposition(&commit_generation)?
                    == OldGenerationCommitDisposition::DiscardForReplacement
                {
                    return Ok(PreparationOutcome::DiscardedByReplacement);
                }
                if let Some(managed) = managed
                    && client.commit_runtime_python(commit_generation.clone(), managed)?
                        == OldGenerationCommitDisposition::DiscardForReplacement
                {
                    return Ok(PreparationOutcome::DiscardedByReplacement);
                }
                if let Some(duckdb_extensions) = python_duckdb
                    && client.commit_running_environment(
                        &commit_generation,
                        None,
                        Some(duckdb_extensions),
                    )? == OldGenerationCommitDisposition::DiscardForReplacement
                {
                    return Ok(PreparationOutcome::DiscardedByReplacement);
                }
                Ok(PreparationOutcome::Completed(Ok(())))
            });
            let result = running.prepare_python(python_packages, includes_r, commit);
            match result {
                Ok(PreparationOutcome::Completed(Ok(()))) => {}
                Ok(PreparationOutcome::Completed(Err(error))) => {
                    return self.fail_running_preparation(
                        &mut worker,
                        generation,
                        false,
                        error,
                        includes_r,
                    );
                }
                Ok(PreparationOutcome::DiscardedByReplacement) => {
                    return Err(preparation_cancelled(includes_r));
                }
                Err(error) => {
                    return self.fail_running_preparation(
                        &mut worker,
                        generation,
                        true,
                        error,
                        includes_r,
                    );
                }
            }
        }
        if let Some(managed_r) = managed_r {
            let library = managed_r.library().to_path_buf();
            let client = self.clone();
            let commit_generation = generation.clone();
            let commit = Box::new(move |result| match result {
                Ok(()) => client
                    .commit_running_environment(
                        &commit_generation,
                        Some(managed_r),
                        duckdb_extensions,
                    )
                    .map(|disposition| match disposition {
                        OldGenerationCommitDisposition::Commit => {
                            PreparationOutcome::Completed(Ok(()))
                        }
                        OldGenerationCommitDisposition::DiscardForReplacement => {
                            PreparationOutcome::DiscardedByReplacement
                        }
                    }),
                Err(error) => client
                    .require_restart_for_requirement_changes(&commit_generation)
                    .map(|_| PreparationOutcome::Completed(Err(error))),
            });
            let result = match running.prepare_r(&library, commit) {
                Ok(result) => result,
                Err(error) => {
                    return self.fail_running_preparation(
                        &mut worker,
                        generation,
                        true,
                        error,
                        true,
                    );
                }
            };
            return match result {
                PreparationOutcome::Completed(Ok(())) => Ok(PrepareResult::Prepared),
                PreparationOutcome::Completed(Err(error)) => {
                    Ok(self.failed_preparation_response(requirement_restart_error(error)))
                }
                PreparationOutcome::DiscardedByReplacement => Err(preparation_cancelled(true)),
            };
        }
        if !includes_python
            && let Err(error) = self.commit_running_environment(generation, None, duckdb_extensions)
        {
            return self.fail_running_preparation(&mut worker, generation, true, error, includes_r);
        }
        Ok(PrepareResult::Prepared)
    }

    fn fail_running_preparation(
        &self,
        worker: &mut std::sync::MutexGuard<'_, WorkerState>,
        generation: &WorkerGeneration,
        stop_worker: bool,
        error: String,
        includes_r: bool,
    ) -> Result<PrepareResult, String> {
        match self.generation_status(generation)? {
            GenerationStatus::Changed => Err(preparation_cancelled(includes_r)),
            GenerationStatus::CurrentReady => {
                if stop_worker {
                    return match self.stop_failed_worker(worker, generation) {
                        Ok(FailedWorkerStop::Stopped(outcome)) => {
                            self.0.output.push_failure(
                                super::output::SendFailure::from(error)
                                    .worker_outcome(outcome)
                                    .worker_stopped(),
                            );
                            Ok(PrepareResult::WorkerStopped(self.0.output.take()))
                        }
                        Ok(FailedWorkerStop::RestartOwnsWorker) => Err(error),
                        Err(stop_error) => {
                            self.0.output.push_failure(
                                stop_error
                                    .attach_to(super::output::SendFailure::from(error))
                                    .worker_stopped(),
                            );
                            Ok(PrepareResult::WorkerStopped(self.0.output.take()))
                        }
                    };
                }
                Ok(self.failed_preparation_response(error))
            }
            GenerationStatus::CurrentClosing => Err(error),
        }
    }

    fn failed_preparation_response(&self, error: String) -> PrepareResult {
        let mut response = self.0.output.take();
        response.push_tool_error(error);
        PrepareResult::Failed(response)
    }

    fn failed_pre_evaluation_resolution(&self, error: String) -> PrepareResult {
        let mut response = self.0.output.take_prelude();
        let mut failure = super::Response::default();
        failure.push_tool_error(error);
        response.extend_cell_after_idle_prelude(failure);
        PrepareResult::Failed(response)
    }

    fn commit_running_environment(
        &self,
        generation: &WorkerGeneration,
        managed_r: Option<crate::resolver::ManagedR>,
        duckdb_extensions: Option<BTreeSet<String>>,
    ) -> Result<OldGenerationCommitDisposition, String> {
        let environment = self
            .0
            .environment
            .as_ref()
            .expect("running managed preparation requires an environment");
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        let disposition = lifecycle.old_generation_commit_disposition(generation)?;
        match disposition {
            OldGenerationCommitDisposition::Commit => {
                if let Some(managed_r) = managed_r {
                    commit_managed_r(&mut environment, managed_r);
                }
                if let Some(duckdb_extensions) = duckdb_extensions {
                    environment.duckdb_extensions = duckdb_extensions;
                }
                Ok(disposition)
            }
            OldGenerationCommitDisposition::DiscardForReplacement => Ok(disposition),
        }
    }

    fn requirement_change_state(
        &self,
        generation: &WorkerGeneration,
    ) -> Result<RequirementChangeState, String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.generation.is(generation) => {
                Ok(lifecycle.requirement_changes)
            }
            LifecycleState::Ready => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    fn require_restart_for_requirement_changes(
        &self,
        generation: &WorkerGeneration,
    ) -> Result<OldGenerationCommitDisposition, String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        let disposition = lifecycle.old_generation_commit_disposition(generation)?;
        match disposition {
            OldGenerationCommitDisposition::Commit => {
                lifecycle.requirement_changes = RequirementChangeState::RestartRequired;
                Ok(disposition)
            }
            OldGenerationCommitDisposition::DiscardForReplacement => Ok(disposition),
        }
    }

    pub(super) fn resolve_runtime_r(
        &self,
        generation: WorkerGeneration,
        packages: Vec<String>,
    ) -> Result<crate::resolver::ManagedR, RuntimeRResolutionFailure> {
        self.ensure_runtime_r_generation(&generation)?;
        crate::r_package_name::validate_all(&packages)
            .map_err(RuntimeRResolutionFailure::Ordinary)?;
        let environment = self.0.environment.as_ref().ok_or_else(|| {
            RuntimeRResolutionFailure::Infrastructure(
                "managed R requirements are unavailable".to_string(),
            )
        })?;
        // This lock serializes the retained-environment snapshot and the one
        // lifecycle-owned host resolver slot through both resolver phases.
        let environment = environment.lock().map_err(|_| {
            RuntimeRResolutionFailure::Infrastructure(
                "worker environment lock poisoned".to_string(),
            )
        })?;
        let (requirements, changed) = merge_r_requirements(&environment, packages);
        if !changed {
            let managed = environment.r.clone().ok_or_else(|| {
                RuntimeRResolutionFailure::Infrastructure(
                    "managed R environment is unavailable".to_string(),
                )
            })?;
            self.ensure_runtime_r_generation(&generation)?;
            return Ok(managed);
        }
        match self
            .requirement_change_state(&generation)
            .map_err(RuntimeRResolutionFailure::Infrastructure)?
        {
            RequirementChangeState::Available => {}
            RequirementChangeState::RestartRequired => {
                return Err(RuntimeRResolutionFailure::Ordinary(
                    "requirement changes are unavailable until session restart".to_string(),
                ));
            }
        }

        let managed = self
            .resolve_managed_r(&generation, requirements)
            .map_err(RuntimeRResolutionFailure::from)?;
        if !environment.duckdb_extensions.is_empty() {
            let extensions = environment
                .duckdb_extensions
                .iter()
                .cloned()
                .collect::<Vec<_>>();
            self.resolve_duckdb_extensions(
                &generation,
                std::slice::from_ref(&managed),
                &extensions,
            )
            .map_err(RuntimeRResolutionFailure::from)?;
        }
        self.ensure_runtime_r_generation(&generation)?;
        Ok(managed)
    }

    pub(super) fn activate_runtime_r(
        &self,
        generation: WorkerGeneration,
        library: String,
        candidates: &mut Vec<crate::resolver::ManagedR>,
    ) -> Result<OldGenerationCommitDisposition, String> {
        let managed = select_r_activation(&library, candidates)?;
        let environment = self
            .0
            .environment
            .as_ref()
            .ok_or_else(|| "managed R environment is unavailable".to_string())?;
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        if lifecycle.state == LifecycleState::Ready && lifecycle.generation.is(&generation) {
            commit_managed_r(&mut environment, managed);
            Ok(OldGenerationCommitDisposition::Commit)
        } else {
            Ok(OldGenerationCommitDisposition::DiscardForReplacement)
        }
    }

    pub(super) fn fail_runtime_r_activation(
        &self,
        generation: WorkerGeneration,
        library: String,
        candidates: &mut Vec<crate::resolver::ManagedR>,
    ) -> Result<OldGenerationCommitDisposition, String> {
        let _managed = select_r_activation(&library, candidates)?;
        let environment = self
            .0
            .environment
            .as_ref()
            .ok_or_else(|| "managed R environment is unavailable".to_string())?;
        let _environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        if lifecycle.state == LifecycleState::Ready && lifecycle.generation.is(&generation) {
            lifecycle.requirement_changes = RequirementChangeState::RestartRequired;
            Ok(OldGenerationCommitDisposition::Commit)
        } else {
            Ok(OldGenerationCommitDisposition::DiscardForReplacement)
        }
    }

    fn ensure_runtime_r_generation(
        &self,
        generation: &WorkerGeneration,
    ) -> Result<(), RuntimeRResolutionFailure> {
        match self.generation_status(generation) {
            Ok(GenerationStatus::CurrentReady) => Ok(()),
            Ok(GenerationStatus::CurrentClosing | GenerationStatus::Changed) => {
                Err(RuntimeRResolutionFailure::Cancelled(
                    "R package resolution cancelled by session lifecycle change".to_string(),
                ))
            }
            Err(error) => Err(RuntimeRResolutionFailure::Infrastructure(error)),
        }
    }

    pub(super) fn resolve_runtime_python(
        &self,
        generation: WorkerGeneration,
        request: crate::worker_protocol::PythonResolveRequest,
    ) -> Result<crate::resolver::ManagedPython, String> {
        self.ensure_generation(&generation)?;
        let environment = self.0.environment.as_ref().ok_or_else(|| {
            "Python requirements are unavailable with a custom worker".to_string()
        })?;
        // Keep the environment locked while the host resolver owns the one lifecycle slot.
        let environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        if environment.custom_worker {
            return Err("Python requirements are unavailable with a custom worker".to_string());
        }
        let (current, resolver) = environment
            .python
            .as_ref()
            .ok_or_else(|| "managed Python environment is unavailable".to_string())?
            .managed_parts()?;
        let current = current.clone();
        crate::python_requirement::validate_all(&request.requirements.packages)?;
        crate::python_requirement::validate_all(&request.retained_requirements.packages)?;
        crate::python_requirement::validate_version_constraints(
            &request.requirements.python_version,
        )?;
        crate::python_requirement::validate_version_constraints(
            &request.retained_requirements.python_version,
        )?;
        let requirements = request.requirements.normalized();
        let retained_requirements = request.retained_requirements.normalized();
        if requirements.packages != retained_requirements.packages
            || requirements.exclude_newer != retained_requirements.exclude_newer
        {
            return Err(
                "Python resolution and retained requirements differ outside the Python version"
                    .to_string(),
            );
        }
        if current.requirements() == &retained_requirements {
            self.ensure_generation(&generation)?;
            return Ok(current);
        }
        match self.requirement_change_state(&generation)? {
            RequirementChangeState::Available => {}
            RequirementChangeState::RestartRequired => {
                return Err("requirement changes are unavailable until session restart".to_string());
            }
        }

        let managed = match crate::resolver::resolve_python_manifest(
            requirements,
            resolver,
            environment.r.as_ref(),
            |handle| self.register_resolver_stop_handle(&generation, handle),
        ) {
            Ok(managed) => managed,
            Err(error) => {
                self.clear_resolver_stop_handle(&generation)?;
                return Err(error);
            }
        };
        self.clear_resolver_stop_handle(&generation)?;
        self.ensure_generation(&generation)?;
        Ok(managed.with_retained_requirements(retained_requirements))
    }

    pub(super) fn resolve_runtime_python_version(
        &self,
        generation: WorkerGeneration,
        request: crate::worker_protocol::PythonVersionResolveRequest,
    ) -> Result<String, String> {
        self.ensure_generation(&generation)?;
        let environment = self.0.environment.as_ref().ok_or_else(|| {
            "Python requirements are unavailable with a custom worker".to_string()
        })?;
        let environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        if environment.custom_worker {
            return Err("Python requirements are unavailable with a custom worker".to_string());
        }
        let (_, resolver) = environment
            .python
            .as_ref()
            .ok_or_else(|| "managed Python environment is unavailable".to_string())?
            .managed_parts()?;
        let result = crate::resolver::resolve_python_version(
            request.constraints,
            resolver,
            environment.r.as_ref(),
            |handle| self.register_resolver_stop_handle(&generation, handle),
        );
        self.clear_resolver_stop_handle(&generation)?;
        self.ensure_generation(&generation)?;
        result
    }

    pub(super) fn activate_runtime_python(
        &self,
        generation: WorkerGeneration,
        requirements: crate::worker_protocol::PythonRequirementManifest,
        candidates: &mut Vec<crate::resolver::ManagedPython>,
    ) -> Result<OldGenerationCommitDisposition, String> {
        let environment = self
            .0
            .environment
            .as_ref()
            .ok_or_else(|| "custom worker reported a managed Python activation".to_string())?;
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        if environment.custom_worker {
            return Err("custom worker reported a managed Python activation".to_string());
        }
        let current = environment
            .python
            .as_ref()
            .ok_or_else(|| "managed Python environment is unavailable".to_string())?
            .managed_parts()?
            .0;
        let managed = select_python_activation(Some(current), requirements, candidates)?;
        candidates.clear();
        self.commit_locked_runtime_python(&generation, &mut environment, managed)
    }

    fn commit_runtime_python(
        &self,
        generation: WorkerGeneration,
        managed: crate::resolver::ManagedPython,
    ) -> Result<OldGenerationCommitDisposition, String> {
        let environment = self
            .0
            .environment
            .as_ref()
            .ok_or_else(|| "managed Python requirements are unavailable".to_string())?;
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        self.commit_locked_runtime_python(&generation, &mut environment, managed)
    }

    fn commit_locked_runtime_python(
        &self,
        generation: &WorkerGeneration,
        environment: &mut Environment,
        managed: crate::resolver::ManagedPython,
    ) -> Result<OldGenerationCommitDisposition, String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        let disposition = lifecycle.old_generation_commit_disposition(generation)?;
        match disposition {
            OldGenerationCommitDisposition::Commit => {
                environment
                    .python
                    .as_mut()
                    .ok_or_else(|| "managed Python environment is unavailable".to_string())?
                    .replace_managed(managed)?;
                Ok(disposition)
            }
            OldGenerationCommitDisposition::DiscardForReplacement => Ok(disposition),
        }
    }

    fn old_generation_commit_disposition(
        &self,
        generation: &WorkerGeneration,
    ) -> Result<OldGenerationCommitDisposition, String> {
        self.0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?
            .old_generation_commit_disposition(generation)
    }
}

fn preparation_cancelled(includes_r: bool) -> String {
    if includes_r {
        "R preparation cancelled by restart".to_string()
    } else {
        "Python preparation cancelled by restart".to_string()
    }
}

fn requirement_restart_error(error: String) -> String {
    format!("{error}; further requirement changes are unavailable until session restart")
}
