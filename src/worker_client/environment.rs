use std::collections::BTreeSet;

use super::lifecycle::{FailedWorkerStop, GenerationStatus, LifecycleState, WorkerGeneration};
use super::{Client, WorkerState};

pub(super) struct Environment {
    pub(super) python: Option<crate::resolver::ManagedPython>,
    pub(super) r: Option<crate::resolver::ManagedR>,
}

pub(crate) enum PrepareResult {
    Prepared,
    RestartRequired,
    WorkerStopped(super::Response),
}

pub(crate) struct Requirements {
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
            .ok_or_else(|| "requirements are unavailable with a custom worker".to_string())?;
        let active_evaluation = self.evaluation()?.is_some();
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        self.ensure_generation(&generation)?;
        let python_additions = requirements.python.into_iter().collect::<BTreeSet<_>>();
        let python_candidate = merge_python_requirements(
            environment.python.as_ref(),
            python_additions.iter().cloned().collect(),
        );
        let r_additions = requirements.r.into_iter().collect::<BTreeSet<_>>();
        let current_r = environment
            .r
            .as_ref()
            .map(|managed| managed.requirements().iter().cloned().collect())
            .unwrap_or_default();
        if python_candidate.is_none() && r_additions.is_subset(&current_r) {
            return Ok(PrepareResult::Prepared);
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
        if matches!(*worker, WorkerState::Stopped)
            || (!matches!(*worker, WorkerState::Initial) && !r_additions.is_subset(&current_r))
        {
            return Ok(PrepareResult::RestartRequired);
        }
        if matches!(*worker, WorkerState::Running(_)) {
            if environment.python.is_none() {
                return Ok(PrepareResult::RestartRequired);
            }
            drop(environment);
            return self.prepare_running_python(
                &generation,
                worker,
                python_additions.into_iter().collect(),
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
                Ok(PrepareResult::Prepared)
            }
            LifecycleState::Ready => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    fn prepare_running_python(
        &self,
        generation: &WorkerGeneration,
        mut worker: std::sync::MutexGuard<'_, WorkerState>,
        packages: Vec<String>,
    ) -> Result<PrepareResult, String> {
        self.ensure_generation(generation)?;
        let WorkerState::Running(running) = &mut *worker else {
            return Err("worker state changed during Python preparation".to_string());
        };
        let result = running.prepare_python(
            packages,
            |request| self.resolve_runtime_python(generation.clone(), request),
            |checkpoint, candidates| {
                self.checkpoint_runtime_python(generation.clone(), Some(checkpoint), candidates)
            },
        );
        let (infrastructure_failure, error) = match result {
            Ok(Ok(())) => return Ok(PrepareResult::Prepared),
            Ok(Err(error)) => (false, error),
            Err(error) => (true, error),
        };
        match self.generation_status(generation)? {
            GenerationStatus::Changed => Err("Python preparation cancelled by restart".to_string()),
            GenerationStatus::CurrentReady => {
                if infrastructure_failure {
                    return match self.stop_failed_worker(&mut worker, generation)? {
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

    pub(super) fn checkpoint_runtime_python(
        &self,
        generation: WorkerGeneration,
        checkpoint: Option<crate::worker_protocol::PythonRequirementManifest>,
        candidates: Vec<crate::resolver::ManagedPython>,
    ) -> Result<(), String> {
        self.ensure_generation(&generation)?;
        let Some(checkpoint) = checkpoint else {
            return if candidates.is_empty() {
                Ok(())
            } else {
                Err("worker resolved Python without reporting a checkpoint".to_string())
            };
        };
        let environment = self
            .0
            .environment
            .as_ref()
            .ok_or_else(|| "custom worker reported a managed Python checkpoint".to_string())?;
        let requirements = checkpoint.normalized();
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        let managed = candidates
            .into_iter()
            .rev()
            .find(|candidate| candidate.requirements() == &requirements)
            .or_else(|| {
                environment
                    .python
                    .clone()
                    .filter(|current| current.requirements() == &requirements)
            })
            .ok_or_else(|| {
                "worker checkpoint does not match a resolved Python environment".to_string()
            })?;
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
