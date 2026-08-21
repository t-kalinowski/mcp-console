use std::collections::BTreeSet;
use std::ffi::OsString;

use super::lifecycle::{
    FailedWorkerStop, GenerationStatus, LifecycleState, OldGenerationCommitDisposition,
    RequirementChangeState, WorkerGeneration,
};
use super::{Client, PreparationOutcome, WorkerState};

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
    ) -> Result<Self, String> {
        if let Some(configured) = configured
            && !configured.is_empty()
            && configured != "managed"
        {
            return Ok(Self::UserSelected(configured));
        }
        let selected = crate::resolver::resolve_python(&[], &resolver, |_| Ok(()))?;
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

pub(crate) struct Requirements {
    pub(crate) duckdb: Vec<String>,
    pub(crate) python: Vec<String>,
    pub(crate) r: Vec<String>,
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
        let _preparation = self.admit_preparation()?;
        let environment = self
            .0
            .environment
            .as_ref()
            .ok_or_else(|| "managed requirements are unavailable".to_string())?;
        let active_operation = self
            .evaluation()?
            .as_ref()
            .map(|active| active.evaluation.clone());
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        self.ensure_generation(&generation)?;
        let Requirements { duckdb, python, r } = requirements;
        ensure_python_additions_available(&environment, &python)?;
        let duckdb_additions = duckdb.into_iter().collect::<BTreeSet<_>>();
        let duckdb_candidate = if duckdb_additions.is_subset(&environment.duckdb_extensions) {
            None
        } else {
            Some(
                environment
                    .duckdb_extensions
                    .union(&duckdb_additions)
                    .cloned()
                    .collect::<BTreeSet<_>>(),
            )
        };
        let python_additions = python.into_iter().collect::<BTreeSet<_>>();
        let python_candidate = merge_python_requirements(
            environment
                .python
                .as_ref()
                .and_then(PythonEnvironment::managed),
            python_additions.iter().cloned().collect(),
        );
        let mut r_additions = r.into_iter().collect::<BTreeSet<_>>();
        if environment.custom_worker {
            r_additions.extend(
                super::CUSTOM_DUCKDB_R_REQUIREMENTS
                    .iter()
                    .map(|requirement| (*requirement).to_string()),
            );
        }
        let current_r = environment
            .r
            .as_ref()
            .map(|managed| managed.requirements().iter().cloned().collect())
            .unwrap_or_default();
        if duckdb_candidate.is_none()
            && python_candidate.is_none()
            && r_additions.is_subset(&current_r)
        {
            return Ok(PrepareResult::Prepared);
        }
        if self.requirement_change_state(&generation)? == RequirementChangeState::RestartRequired {
            return Ok(PrepareResult::RestartRequired);
        }
        if let Some(active) = active_operation {
            return Err(active.reject_preparation_message().to_string());
        }
        let worker = match self.0.worker.try_lock() {
            Ok(worker) => worker,
            Err(std::sync::TryLockError::WouldBlock) => {
                return Err("[requirements not prepared: worker is starting]".to_string());
            }
            Err(std::sync::TryLockError::Poisoned(_)) => {
                return Err("worker lock poisoned".to_string());
            }
        };
        if matches!(*worker, WorkerState::Stopped) {
            return Ok(PrepareResult::RestartRequired);
        }
        if matches!(*worker, WorkerState::Running(_)) {
            let managed_r = if r_additions.is_subset(&current_r) {
                None
            } else {
                let requirements = current_r.union(&r_additions).cloned().collect();
                let result = crate::resolver::resolve_r(requirements, |handle| {
                    self.register_resolver_stop_handle(&generation, handle)
                });
                self.clear_resolver_stop_handle(&generation)?;
                Some(result?)
            };
            let duckdb_extensions = duckdb_candidate
                .as_ref()
                .unwrap_or(&environment.duckdb_extensions);
            if !duckdb_extensions.is_empty() && (duckdb_candidate.is_some() || managed_r.is_some())
            {
                let mut targets = Vec::new();
                if duckdb_candidate.is_some() {
                    targets.extend(environment.duckdb_r_targets.iter().cloned());
                }
                if let Some(managed_r) = managed_r.as_ref() {
                    push_duckdb_r_target(&mut targets, managed_r.clone());
                }
                let duckdb_extensions = duckdb_extensions.iter().cloned().collect::<Vec<_>>();
                self.resolve_duckdb_extensions(&generation, &targets, &duckdb_extensions)?;
            }
            let python_packages = if python_candidate.is_some() {
                python_additions.into_iter().collect()
            } else {
                Vec::new()
            };
            if python_packages.is_empty() && managed_r.is_none() {
                if let Some(duckdb_candidate) = duckdb_candidate {
                    self.commit_locked_duckdb_environment(
                        &generation,
                        &mut environment,
                        duckdb_candidate,
                    )?;
                }
                return Ok(PrepareResult::Prepared);
            }
            drop(environment);
            return self.prepare_running(
                &generation,
                worker,
                python_packages,
                managed_r,
                duckdb_candidate,
            );
        }

        let r_requirements = current_r.union(&r_additions).cloned().collect::<Vec<_>>();

        let mut managed_r = environment.r.clone();
        if !r_additions.is_subset(&current_r) {
            let result = crate::resolver::resolve_r(r_requirements, |handle| {
                self.register_resolver_stop_handle(&generation, handle)
            });
            self.clear_resolver_stop_handle(&generation)?;
            managed_r = Some(result?);
        }

        let duckdb_extensions = duckdb_candidate
            .as_ref()
            .unwrap_or(&environment.duckdb_extensions);
        if !duckdb_extensions.is_empty()
            && (duckdb_candidate.is_some() || !r_additions.is_subset(&current_r))
        {
            let mut targets = Vec::new();
            if let Some(managed_r) = managed_r.as_ref() {
                push_duckdb_r_target(&mut targets, managed_r.clone());
            }
            let duckdb_extensions = duckdb_extensions.iter().cloned().collect::<Vec<_>>();
            self.resolve_duckdb_extensions(&generation, &targets, &duckdb_extensions)?;
        }

        let managed_python = if let Some(candidate) = python_candidate {
            let (_, resolver) = environment
                .python
                .as_ref()
                .ok_or_else(|| "managed Python environment is unavailable".to_string())?
                .managed_parts()?;
            let resolver = resolver.clone();
            let result = crate::resolver::resolve_python_host(candidate, &resolver, |handle| {
                self.register_resolver_stop_handle(&generation, handle)
            });
            self.clear_resolver_stop_handle(&generation)?;
            Some(result?)
        } else {
            None
        };

        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.generation.is(&generation) => {
                lifecycle.processes.resolver = None;
                if let Some(managed_python) = managed_python {
                    environment
                        .python
                        .as_mut()
                        .ok_or_else(|| "managed Python environment is unavailable".to_string())?
                        .replace_managed(managed_python)?;
                }
                environment.r = managed_r;
                if let Some(duckdb_candidate) = duckdb_candidate {
                    environment.duckdb_extensions = duckdb_candidate;
                }
                Ok(PrepareResult::Prepared)
            }
            LifecycleState::Ready => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    pub(super) fn resolve_duckdb_extensions(
        &self,
        generation: &WorkerGeneration,
        managed_r: &[crate::resolver::ManagedR],
        extensions: &[String],
    ) -> Result<(), String> {
        if managed_r.is_empty() {
            return Err(
                "DuckDB extension preparation requires a managed R environment".to_string(),
            );
        }
        for managed_r in managed_r {
            let result =
                crate::resolver::resolve_duckdb_extensions(managed_r, extensions, |handle| {
                    self.register_resolver_stop_handle(generation, handle)
                });
            self.clear_resolver_stop_handle(generation)?;
            result?;
        }
        Ok(())
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
            let result = running.prepare_python(python_packages, commit);
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
                    push_duckdb_r_target(&mut environment.duckdb_r_targets, managed_r.clone());
                    environment.r = Some(managed_r);
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

        let managed =
            match crate::resolver::resolve_python_manifest(requirements, resolver, |handle| {
                self.register_resolver_stop_handle(&generation, handle)
            }) {
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
        let result =
            crate::resolver::resolve_python_version(request.constraints, resolver, |handle| {
                self.register_resolver_stop_handle(&generation, handle)
            });
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

#[cfg(test)]
mod tests {
    use std::ffi::OsString;

    use super::{
        Environment, PythonEnvironment, USER_SELECTED_PYTHON_ERROR,
        ensure_python_additions_available,
    };

    fn environment(custom_worker: bool, python: Option<PythonEnvironment>) -> Environment {
        Environment {
            custom_worker,
            duckdb_extensions: Default::default(),
            duckdb_r_targets: Vec::new(),
            python,
            r: None,
        }
    }

    #[test]
    fn user_selected_python_has_a_distinct_managed_requirement_policy() {
        let environment = environment(
            false,
            Some(PythonEnvironment::UserSelected(OsString::from(
                "/selected/python",
            ))),
        );
        assert_eq!(
            ensure_python_additions_available(&environment, &["numpy".to_string()]),
            Err(USER_SELECTED_PYTHON_ERROR.to_string())
        );
        assert_eq!(
            environment.python.as_ref().unwrap().managed_parts().err(),
            Some(USER_SELECTED_PYTHON_ERROR.to_string())
        );
    }

    #[test]
    fn custom_worker_policy_remains_separate() {
        let environment = environment(true, None);
        assert_eq!(
            ensure_python_additions_available(&environment, &["numpy".to_string()]),
            Err("Python requirements are unavailable with a custom worker".to_string())
        );
    }
}
