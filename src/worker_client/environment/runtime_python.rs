use super::super::Client;
use super::super::lifecycle::{
    OldGenerationCommitDisposition, RequirementChangeState, WorkerGeneration,
};
use super::requirements::{select_python_activation, validate_python_import_resolution};
use super::state::Environment;

impl Client {
    pub(in crate::worker_client) fn resolve_runtime_python(
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
        let crate::worker_protocol::PythonResolveRequest {
            requirements,
            retained_requirements,
            import_resolution,
        } = request;
        crate::python_requirement::validate_all(&requirements.packages)?;
        crate::python_requirement::validate_all(&retained_requirements.packages)?;
        crate::python_requirement::validate_version_constraints(&requirements.python_version)?;
        crate::python_requirement::validate_version_constraints(
            &retained_requirements.python_version,
        )?;
        let requirements = requirements.normalized();
        let retained_requirements = retained_requirements.normalized();
        if requirements.packages != retained_requirements.packages
            || requirements.exclude_newer != retained_requirements.exclude_newer
        {
            return Err(
                "Python resolution and retained requirements differ outside the Python version"
                    .to_string(),
            );
        }
        if let Some(resolution) = import_resolution.as_ref() {
            validate_python_import_resolution(resolution, &retained_requirements)?;
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

    pub(in crate::worker_client) fn resolve_runtime_python_version(
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

    pub(in crate::worker_client) fn activate_runtime_python(
        &self,
        generation: WorkerGeneration,
        requirements: crate::worker_protocol::PythonRequirementManifest,
        candidate: Option<crate::resolver::ManagedPython>,
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
        let managed = select_python_activation(Some(current), requirements, candidate)?;
        self.commit_locked_runtime_python(&generation, &mut environment, managed)
    }

    pub(super) fn commit_runtime_python(
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

    pub(super) fn old_generation_commit_disposition(
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
