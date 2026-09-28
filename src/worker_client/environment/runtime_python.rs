use super::super::Client;
use super::super::lifecycle::{
    OldGenerationCommitDisposition, RequirementChangeState, WorkerGeneration,
};
use super::requirements::{
    RequirementDelta, Requirements, select_python_activation, validate_python_import_resolution,
};
use super::state::Environment;

impl Client {
    pub(in crate::worker_client) fn resolve_runtime_python(
        &self,
        generation: WorkerGeneration,
        request: crate::worker_protocol::PythonResolveRequest,
    ) -> Result<super::super::PythonCandidate, String> {
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
        if environment
            .local_runtime
            .as_ref()
            .is_some_and(crate::local_runtime::Selection::python_only)
        {
            let resolution = import_resolution
                .as_ref()
                .ok_or_else(|| "native Python resolution requires a reached import".to_string())?;
            let delta = RequirementDelta::calculate(
                &environment,
                Requirements {
                    python: vec![resolution.distribution.clone()],
                    ..Default::default()
                },
            )?;
            delta.validate_live_python_additions(&environment)?;
            let expected = delta.python_candidate.ok_or_else(|| {
                "automatic Python import did not add a new distribution".to_string()
            })?;
            if expected != requirements || expected != retained_requirements {
                return Err(
                    "automatic Python import does not match the retained declaration".into(),
                );
            }
            if self.requirement_change_state(&generation)?
                == RequirementChangeState::RestartRequired
            {
                return Err("requirement changes are unavailable until session restart".into());
            }
            let (candidate, inspected) = self
                .resolve_live_native_python(
                    &generation,
                    &environment,
                    expected,
                    &environment.duckdb_extensions,
                )
                .map_err(|failure| failure.into_message())?;
            return Ok((candidate, inspected));
        }
        if current.requirements() == &retained_requirements {
            self.ensure_generation(&generation)?;
            let inspected = self
                .inspect_managed_python(&generation, &current, resolver)
                .map_err(|failure| failure.into_message())?;
            return Ok((current, inspected));
        }
        match self.requirement_change_state(&generation)? {
            RequirementChangeState::Available => {}
            RequirementChangeState::RestartRequired => {
                return Err("requirement changes are unavailable until session restart".to_string());
            }
        }

        let managed = match crate::resolver::execution::resolve_python_manifest(
            requirements,
            resolver,
            environment.r.as_ref(),
            None,
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
        let inspected = self
            .inspect_managed_python(&generation, &managed, resolver)
            .map_err(|failure| failure.into_message())?;
        Ok((
            managed.with_retained_requirements(retained_requirements),
            inspected,
        ))
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
        let result = crate::resolver::execution::resolve_python_version(
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
        configuration: Option<crate::python::NativePython>,
        duckdb_extensions: Option<std::collections::BTreeSet<String>>,
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
        self.commit_locked_runtime_python(
            &generation,
            &mut environment,
            managed,
            configuration,
            duckdb_extensions,
        )
    }

    pub(super) fn commit_runtime_python(
        &self,
        generation: WorkerGeneration,
        managed: crate::resolver::ManagedPython,
        configuration: crate::python::NativePython,
    ) -> Result<OldGenerationCommitDisposition, String> {
        let environment = self
            .0
            .environment
            .as_ref()
            .ok_or_else(|| "managed Python requirements are unavailable".to_string())?;
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        self.commit_locked_runtime_python(
            &generation,
            &mut environment,
            managed,
            Some(configuration),
            None,
        )
    }

    fn commit_locked_runtime_python(
        &self,
        generation: &WorkerGeneration,
        environment: &mut Environment,
        managed: crate::resolver::ManagedPython,
        configuration: Option<crate::python::NativePython>,
        duckdb_extensions: Option<std::collections::BTreeSet<String>>,
    ) -> Result<OldGenerationCommitDisposition, String> {
        if let Some(configuration) = configuration.as_ref()
            && (environment.local_runtime.is_none()
                || environment
                    .local_runtime
                    .as_ref()
                    .and_then(|runtime| runtime.python.as_ref())
                    .is_some_and(|python| !python.managed)
                || std::path::Path::new(&configuration.embedding.python) != managed.python())
        {
            return Err(
                "worker activation does not match the approved native Python candidate".into(),
            );
        }
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
                if let Some(configuration) = configuration {
                    let runtime = environment
                        .local_runtime
                        .as_mut()
                        .expect("candidate runtime validated");
                    match &mut runtime.python {
                        Some(python) => *python.selected = configuration,
                        None => {
                            runtime.python = Some(crate::local_runtime::Python {
                                selected: Box::new(configuration),
                                explicit: None,
                                managed: true,
                                duckdb_extension_directory: None,
                            })
                        }
                    }
                    self.record_accepted_python(environment);
                }
                if let Some(duckdb_extensions) = duckdb_extensions {
                    environment.duckdb_extensions = duckdb_extensions;
                }
                self.publish_requirements(environment);
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
