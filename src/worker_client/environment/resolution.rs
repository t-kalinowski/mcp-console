use super::super::lifecycle::WorkerGeneration;
use super::super::{Client, RResolver};
use super::requirements::RequirementDelta;
use super::state::{Environment, PythonEnvironment};

pub(in crate::worker_client) enum EnvironmentResolutionFailure {
    Host(String),
    Interrupted(String),
    Cancelled(String),
    Operation(String),
}

impl EnvironmentResolutionFailure {
    pub(in crate::worker_client) fn into_message(self) -> String {
        match self {
            Self::Host(message)
            | Self::Interrupted(message)
            | Self::Cancelled(message)
            | Self::Operation(message) => message,
        }
    }
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
    pub(in crate::worker_client) fn resolve_prestart_environment(
        &self,
        generation: &WorkerGeneration,
        environment: &Environment,
        delta: RequirementDelta,
    ) -> Result<Environment, EnvironmentResolutionFailure> {
        let RequirementDelta {
            duckdb_extensions,
            duckdb_changed,
            python_additions: _,
            python_candidate,
            r_requirements,
            r_changed,
        } = delta;
        let mut environment = environment.clone();
        let pending_python = if let RResolver::Pending(setup) = &environment.r_resolver {
            let mut python = setup.python_resolver.clone();
            let mut stop_handle = None;
            let result = setup.bootstrap.prepare(
                &mut python,
                |handle: crate::resolver::ResolverStopHandle| {
                    stop_handle = Some(handle.clone());
                    self.register_resolver_stop_handle(generation, handle)
                },
            );
            self.clear_resolver_stop_handle(generation)
                .map_err(EnvironmentResolutionFailure::Operation)?;
            let resolver = classify_resolver_result(result, stop_handle.as_ref())?;
            let python = if PythonEnvironment::uses_managed(setup.configured_python.as_deref()) {
                Some(python)
            } else {
                environment.python = Some(PythonEnvironment::bare(setup.configured_python.clone()));
                None
            };
            environment.r_resolver = RResolver::Configured(resolver);
            python
        } else {
            None
        };
        if r_changed {
            environment.r = Some(self.resolve_managed_r(
                generation,
                &environment.r_resolver,
                r_requirements,
            )?);
        }
        if !duckdb_extensions.is_empty() && (duckdb_changed || r_changed) {
            let target = environment.r.as_ref().ok_or_else(|| {
                EnvironmentResolutionFailure::Operation(
                    "DuckDB extension preparation requires a managed R environment".to_string(),
                )
            })?;
            let extensions = duckdb_extensions.iter().cloned().collect::<Vec<_>>();
            self.resolve_duckdb_extensions(generation, std::slice::from_ref(target), &extensions)?;
        }
        if let Some(candidate) = python_candidate {
            let mut resolver = match pending_python {
                Some(resolver) => resolver,
                None => environment
                    .python
                    .as_ref()
                    .ok_or_else(|| {
                        EnvironmentResolutionFailure::Operation(
                            "managed Python environment is unavailable".to_string(),
                        )
                    })?
                    .managed_parts()
                    .map_err(EnvironmentResolutionFailure::Operation)?
                    .1
                    .clone(),
            };
            if !resolver.has_uv() {
                let RResolver::Configured(r_resolver) = &environment.r_resolver else {
                    unreachable!("managed Python bootstrap requires a configured R resolver");
                };
                let managed_r = environment
                    .r
                    .as_ref()
                    .expect("managed Python bootstrap has resolved R");
                let mut stop_handle = None;
                let result = r_resolver.resolve_uv(
                    managed_r,
                    &resolver,
                    |handle: crate::resolver::ResolverStopHandle| {
                        stop_handle = Some(handle.clone());
                        self.register_resolver_stop_handle(generation, handle)
                    },
                );
                self.clear_resolver_stop_handle(generation)
                    .map_err(EnvironmentResolutionFailure::Operation)?;
                resolver.set_resolved_uv(classify_resolver_result(result, stop_handle.as_ref())?);
            }
            let selected = self.resolve_managed_python_host(
                generation,
                candidate,
                &resolver,
                environment.r.as_ref(),
            )?;
            environment.python = Some(PythonEnvironment::Managed { selected, resolver });
        }
        environment.duckdb_extensions = duckdb_extensions;
        Ok(environment)
    }

    pub(super) fn resolve_managed_r(
        &self,
        generation: &WorkerGeneration,
        resolver: &RResolver,
        requirements: Vec<String>,
    ) -> Result<crate::resolver::ManagedR, EnvironmentResolutionFailure> {
        let mut stop_handle = None;
        let on_started = |handle: crate::resolver::ResolverStopHandle| {
            stop_handle = Some(handle.clone());
            self.register_resolver_stop_handle(generation, handle)
        };
        let result = match resolver {
            super::super::RResolver::Discover => {
                crate::resolver::resolve_r(requirements, on_started)
            }
            super::super::RResolver::Configured(configuration) => {
                crate::resolver::resolve_r_with(configuration, requirements, on_started)
            }
            super::super::RResolver::Disabled => {
                return Err(EnvironmentResolutionFailure::Host(
                    "dynamic environment resolution is unavailable; install `ir` or `uv` and restart MCP Console"
                        .to_string(),
                ));
            }
            super::super::RResolver::Pending(_) => {
                unreachable!("built-in bootstrap must be prepared before R resolution")
            }
        };
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
        let mut stop_handle = None;
        let result =
            crate::resolver::resolve_python_host(requirements, resolver, managed_r, |handle| {
                stop_handle = Some(handle.clone());
                self.register_resolver_stop_handle(generation, handle)
            });
        self.clear_resolver_stop_handle(generation)
            .map_err(EnvironmentResolutionFailure::Operation)?;
        classify_resolver_result(result, stop_handle.as_ref())
    }

    pub(super) fn resolve_duckdb_extensions(
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
}
