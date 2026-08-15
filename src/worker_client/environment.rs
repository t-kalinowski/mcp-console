use std::collections::BTreeSet;

use super::lifecycle::{
    FailedWorkerStop, GenerationStatus, LifecycleState, RequirementChangeState, WorkerGeneration,
};
use super::{Client, WorkerState};

pub(super) struct Environment {
    pub(super) custom_worker: bool,
    pub(super) duckdb_extensions: BTreeSet<String>,
    /// R libraries that may have supplied DuckDB in the current worker generation.
    pub(super) duckdb_r_targets: Vec<crate::resolver::ManagedR>,
    pub(super) python: Option<crate::resolver::ManagedPython>,
    pub(super) r: Option<crate::resolver::ManagedR>,
}

pub(crate) enum PrepareResult {
    Prepared,
    RestartRequired,
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
    candidates: &[crate::resolver::ManagedPython],
) -> Result<crate::resolver::ManagedPython, String> {
    let requirements = requirements.normalized();
    candidates
        .iter()
        .rev()
        .find(|candidate| candidate.requirements() == &requirements)
        .cloned()
        .or_else(|| {
            current
                .cloned()
                .filter(|current| current.requirements() == &requirements)
        })
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
        let active_evaluation = self.evaluation()?.is_some();
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        self.ensure_generation(&generation)?;
        let Requirements { duckdb, python, r } = requirements;
        if environment.custom_worker && !python.is_empty() {
            return Err("Python requirements are unavailable with a custom worker".to_string());
        }
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
            environment.python.as_ref(),
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
        if self.requirement_change_state(&generation)?.0 == RequirementChangeState::RestartRequired
        {
            return Ok(PrepareResult::RestartRequired);
        }
        if active_evaluation {
            return Err(
                "worker is already evaluating a cell; poll it before preparing requirements"
                    .to_string(),
            );
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
            if python_candidate.is_some() && environment.python.is_none() {
                return Ok(PrepareResult::RestartRequired);
            }
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

        let mut managed_python = environment.python.clone();
        if let Some(candidate) = python_candidate {
            let result = crate::resolver::resolve_python_host(candidate, |handle| {
                self.register_resolver_stop_handle(&generation, handle)
            });
            self.clear_resolver_stop_handle(&generation)?;
            managed_python = Some(result?);
        }

        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.generation.is(&generation) => {
                lifecycle.processes.resolver = None;
                environment.python = managed_python;
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
        let managed_python = if python_packages.is_empty() {
            None
        } else {
            let result = running.prepare_python(
                python_packages,
                |request| self.resolve_runtime_python(generation.clone(), request),
                |requirements, candidates| {
                    self.activate_runtime_python(generation.clone(), requirements, candidates)
                },
            );
            match result {
                Ok(Ok(Some(managed))) => {
                    if let Err(error) =
                        self.commit_runtime_python(generation.clone(), managed.clone())
                    {
                        return self.fail_running_preparation(
                            &mut worker,
                            generation,
                            true,
                            error,
                            includes_r,
                        );
                    }
                    Some(managed)
                }
                Ok(Ok(None)) => None,
                Ok(Err(error)) => {
                    return self.fail_running_preparation(
                        &mut worker,
                        generation,
                        false,
                        error,
                        includes_r,
                    );
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
        };
        if let Some(managed_r) = managed_r.as_ref() {
            match running.prepare_r(managed_r.library()) {
                Ok(Ok(())) => {}
                Ok(Err(error)) => {
                    self.require_restart_for_requirement_changes(
                        generation,
                        managed_python.clone(),
                    )?;
                    return Err(requirement_restart_error(error));
                }
                Err(error) => {
                    return self.fail_running_preparation(
                        &mut worker,
                        generation,
                        true,
                        error,
                        true,
                    );
                }
            }
        }
        if let Err(error) = self.commit_running_environment(
            generation,
            managed_python,
            managed_r,
            duckdb_extensions,
        ) {
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
            GenerationStatus::Changed => Err(if includes_r {
                "R preparation cancelled by restart".to_string()
            } else {
                "Python preparation cancelled by restart".to_string()
            }),
            GenerationStatus::CurrentReady => {
                if stop_worker {
                    return match self.stop_failed_worker(worker, generation)? {
                        FailedWorkerStop::Stopped => {
                            self.0.output.push_failure(
                                super::output::SendFailure::from(error).worker_stopped(),
                            );
                            Ok(PrepareResult::WorkerStopped(self.0.output.take()))
                        }
                        FailedWorkerStop::RestartOwnsWorker => Err(error),
                    };
                }
                Err(error)
            }
            GenerationStatus::CurrentClosing => Err(error),
        }
    }

    fn commit_running_environment(
        &self,
        generation: &WorkerGeneration,
        managed_python: Option<crate::resolver::ManagedPython>,
        managed_r: Option<crate::resolver::ManagedR>,
        duckdb_extensions: Option<BTreeSet<String>>,
    ) -> Result<(), String> {
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
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.generation.is(generation) => {
                if let Some(managed_python) = managed_python {
                    environment.python = Some(managed_python);
                }
                if let Some(managed_r) = managed_r {
                    push_duckdb_r_target(&mut environment.duckdb_r_targets, managed_r.clone());
                    environment.r = Some(managed_r);
                }
                if let Some(duckdb_extensions) = duckdb_extensions {
                    environment.duckdb_extensions = duckdb_extensions;
                }
                Ok(())
            }
            LifecycleState::Ready => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    fn requirement_change_state(
        &self,
        generation: &WorkerGeneration,
    ) -> Result<
        (
            RequirementChangeState,
            Option<crate::resolver::ManagedPython>,
        ),
        String,
    > {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.generation.is(generation) => Ok((
                lifecycle.requirement_changes,
                lifecycle.provisional_python.clone(),
            )),
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
        provisional_python: Option<crate::resolver::ManagedPython>,
    ) -> Result<(), String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.generation.is(generation) => {
                lifecycle.requirement_changes = RequirementChangeState::RestartRequired;
                lifecycle.provisional_python = provisional_python;
                Ok(())
            }
            LifecycleState::Ready => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
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
        let current = environment.python.clone().ok_or_else(|| {
            "runtime Python requirements require a server-managed interpreter".to_string()
        })?;
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
            (RequirementChangeState::Available, _) => {}
            (RequirementChangeState::RestartRequired, Some(provisional))
                if provisional.requirements() == &retained_requirements =>
            {
                self.ensure_generation(&generation)?;
                return Ok(provisional);
            }
            (RequirementChangeState::RestartRequired, _) => {
                return Err("requirement changes are unavailable until session restart".to_string());
            }
        }

        let managed = match crate::resolver::resolve_python_manifest(
            requirements,
            request.environment,
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
        if environment.python.is_none() {
            return Err(
                "runtime Python version resolution requires a server-managed interpreter"
                    .to_string(),
            );
        }
        let result = crate::resolver::resolve_python_version(
            request.constraints,
            request.environment,
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
        candidates: &[crate::resolver::ManagedPython],
    ) -> Result<(), String> {
        self.ensure_generation(&generation)?;
        let environment = self
            .0
            .environment
            .as_ref()
            .ok_or_else(|| "custom worker reported a managed Python activation".to_string())?;
        let environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        if environment.custom_worker {
            return Err("custom worker reported a managed Python activation".to_string());
        }
        let managed =
            select_python_activation(environment.python.as_ref(), requirements, candidates)?;
        drop(environment);
        self.commit_runtime_python(generation, managed)
    }

    fn commit_runtime_python(
        &self,
        generation: WorkerGeneration,
        managed: crate::resolver::ManagedPython,
    ) -> Result<(), String> {
        self.ensure_generation(&generation)?;
        let environment = self
            .0
            .environment
            .as_ref()
            .ok_or_else(|| "managed Python requirements are unavailable".to_string())?;
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.generation.is(&generation) => {
                environment.python = Some(managed);
                Ok(())
            }
            LifecycleState::Ready => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }
}

fn requirement_restart_error(error: String) -> String {
    format!("{error}; further requirement changes are unavailable until session restart")
}
