use std::collections::BTreeSet;

use super::super::Client;
use super::super::lifecycle::WorkerGeneration;
use super::requirements::RequirementDelta;
use super::state::Environment;

pub(in crate::worker_client) struct ResolvedEnvironment {
    pub(in crate::worker_client) duckdb_extensions: BTreeSet<String>,
    pub(in crate::worker_client) managed_python: Option<crate::resolver::ManagedPython>,
    pub(in crate::worker_client) managed_r: Option<crate::resolver::ManagedR>,
}

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

    pub(super) fn resolve_managed_r(
        &self,
        generation: &WorkerGeneration,
        requirements: Vec<String>,
    ) -> Result<crate::resolver::ManagedR, EnvironmentResolutionFailure> {
        let mut stop_handle = None;
        let on_started = |handle: crate::resolver::ResolverStopHandle| {
            stop_handle = Some(handle.clone());
            self.register_resolver_stop_handle(generation, handle)
        };
        let result = match &self.0.r_resolver {
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
        let result =
            crate::resolver::resolve_python_host(requirements, resolver, managed_r, |handle| {
                self.register_resolver_stop_handle(generation, handle)
            });
        self.clear_resolver_stop_handle(generation)
            .map_err(EnvironmentResolutionFailure::Operation)?;
        result.map_err(EnvironmentResolutionFailure::Host)
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
