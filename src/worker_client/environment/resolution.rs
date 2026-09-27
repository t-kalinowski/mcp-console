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
    result.map_err(|message| {
        if handle.is_some_and(|handle| !handle.cleanup_confirmed()) {
            EnvironmentResolutionFailure::Operation(message)
        } else {
            match handle.and_then(|handle| handle.control_outcome()) {
                Some(crate::resolver::ResolverControlOutcome::Interrupted) => {
                    EnvironmentResolutionFailure::Interrupted(message)
                }
                Some(crate::resolver::ResolverControlOutcome::Cancelled) => {
                    EnvironmentResolutionFailure::Cancelled(message)
                }
                None => EnvironmentResolutionFailure::Host(message),
            }
        }
    })
}

impl Client {
    pub(in crate::worker_client) fn resolve_prestart_environment(
        &self,
        generation: &WorkerGeneration,
        environment: &Environment,
        delta: RequirementDelta,
    ) -> Result<Environment, EnvironmentResolutionFailure> {
        self.ensure_startup(generation)
            .map_err(EnvironmentResolutionFailure::Operation)?;
        let RequirementDelta {
            duckdb_extensions,
            duckdb_changed,
            python_additions: _,
            restart_required: _,
            python_candidate,
            r_requirements,
            r_changed,
        } = delta;
        let mut environment = environment.clone();
        let early_resolver = match &environment.r_resolver {
            RResolver::Pending(setup) => Some(&setup.python_resolver),
            _ => match environment.python.as_ref() {
                Some(PythonEnvironment::Managed { resolver, .. }) => Some(resolver),
                _ => None,
            },
        };
        let early_python = match (&python_candidate, early_resolver) {
            (Some(candidate), Some(resolver)) if resolver.direct_uv => Some(
                self.resolve_managed_python_host(generation, candidate.clone(), resolver, None)?,
            ),
            _ => None,
        };
        let pending_python = if let RResolver::Pending(setup) = &environment.r_resolver {
            let python = setup.python_resolver.clone();
            let mut stop_handle = None;
            let result = setup
                .bootstrap
                .prepare(|handle: crate::resolver::ResolverStopHandle| {
                    stop_handle = Some(handle.clone());
                    self.register_resolver_stop_handle(generation, handle)
                });
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
            let resolver = match pending_python {
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
            let selected = if let Some(selected) = early_python {
                selected
            } else {
                self.resolve_managed_python_host(
                    generation,
                    candidate,
                    &resolver,
                    environment.r.as_ref(),
                )?
            };
            if let Some(crate::local_runtime::Selection::Python {
                selected: inspected,
                managed,
                ..
            }) = &mut environment.local_runtime
            {
                **inspected = selected
                    .native()
                    .ok_or_else(|| {
                        EnvironmentResolutionFailure::Operation(
                            "resolver did not return Python embedding configuration".into(),
                        )
                    })?
                    .clone();
                *managed = Some(selected.clone());
            }
            environment.python = Some(PythonEnvironment::Managed { selected, resolver });
        }
        self.ensure_startup(generation)
            .map_err(EnvironmentResolutionFailure::Operation)?;
        environment.duckdb_extensions = duckdb_extensions;
        Ok(environment)
    }

    pub(super) fn resolve_managed_r(
        &self,
        generation: &WorkerGeneration,
        resolver: &RResolver,
        requirements: Vec<String>,
    ) -> Result<crate::resolver::ManagedR, EnvironmentResolutionFailure> {
        self.ensure_startup(generation)
            .map_err(EnvironmentResolutionFailure::Operation)?;
        let mut stop_handle = None;
        let on_started = |handle: crate::resolver::ResolverStopHandle| {
            stop_handle = Some(handle.clone());
            self.register_resolver_stop_handle(generation, handle)
        };
        let retained = requirements.clone();
        let requirements = requirements
            .into_iter()
            .chain(self.0.runtime_r_requirements.iter().cloned())
            .collect::<std::collections::BTreeSet<_>>()
            .into_iter()
            .collect();
        let result = match resolver {
            super::super::RResolver::Discover => self
                .0
                .resolver
                .as_ref()
                .expect("local resolver broker")
                .call(
                    crate::resolver::preparation::Operation::R { requirements },
                    on_started,
                ),
            super::super::RResolver::Configured(configuration) => {
                configuration.resolve_r(requirements, on_started)
            }
            super::super::RResolver::Disabled => {
                let message = if let Some(session) = &self.0.target
                    && !session.is_ssh()
                {
                    format!(
                        "dynamic environment resolution is unavailable for {} targets; install packages in the image and start a new server session",
                        session.protocol().0
                    )
                } else {
                    "dynamic environment resolution is unavailable; install `ir` or `uv` and restart MCP Console".into()
                };
                return Err(EnvironmentResolutionFailure::Host(message));
            }
            super::super::RResolver::Pending(_) => {
                unreachable!("built-in bootstrap must be prepared before R resolution")
            }
        };
        self.clear_resolver_stop_handle(generation)
            .map_err(EnvironmentResolutionFailure::Operation)?;
        classify_resolver_result(result, stop_handle.as_ref())
            .map(|managed| managed.with_retained_requirements(retained))
    }

    fn resolve_managed_python_host(
        &self,
        generation: &WorkerGeneration,
        requirements: crate::worker_protocol::PythonRequirementManifest,
        resolver: &crate::resolver::execution::PythonConfiguration,
        managed_r: Option<&crate::resolver::ManagedR>,
    ) -> Result<crate::resolver::ManagedPython, EnvironmentResolutionFailure> {
        self.ensure_startup(generation)
            .map_err(EnvironmentResolutionFailure::Operation)?;
        let mut stop_handle = None;
        let result = crate::resolver::execution::resolve_python_manifest(
            requirements,
            resolver,
            managed_r,
            |handle| {
                stop_handle = Some(handle.clone());
                self.register_resolver_stop_handle(generation, handle)
            },
        );
        self.clear_resolver_stop_handle(generation)
            .map_err(EnvironmentResolutionFailure::Operation)?;
        self.ensure_startup(generation)
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
            self.ensure_startup(generation)
                .map_err(EnvironmentResolutionFailure::Operation)?;
            let mut stop_handle = None;
            let result = crate::resolver::execution::resolve_duckdb_extensions(
                self.0
                    .target
                    .as_ref()
                    .and_then(crate::target_session::Session::ssh_preparation)
                    .or(self.0.resolver.as_ref())
                    .ok_or_else(|| {
                        EnvironmentResolutionFailure::Operation(
                            "resolver broker is unavailable".into(),
                        )
                    })?,
                managed_r,
                extensions,
                |handle| {
                    stop_handle = Some(handle.clone());
                    self.register_resolver_stop_handle(generation, handle)
                },
            );
            self.clear_resolver_stop_handle(generation)
                .map_err(EnvironmentResolutionFailure::Operation)?;
            classify_resolver_result(result, stop_handle.as_ref())?;
        }
        Ok(())
    }
}
