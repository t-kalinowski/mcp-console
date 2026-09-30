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
        let python_only = environment
            .local_runtime
            .as_ref()
            .is_some_and(crate::local_runtime::Selection::python_only);
        let python_changed = python_candidate.is_some();
        let early_resolver = match &environment.r_resolver {
            RResolver::Pending(setup) => Some(&setup.python_resolver),
            _ => match environment.python.as_ref() {
                Some(PythonEnvironment::Managed { resolver, .. }) => Some(resolver),
                _ => None,
            },
        };
        let early_python = match (&python_candidate, early_resolver) {
            (Some(candidate), Some(resolver)) if resolver.has_direct_local_uv() => {
                Some(self.resolve_managed_python_host(
                    generation,
                    candidate.clone(),
                    resolver,
                    None,
                    None,
                )?)
            }
            _ => None,
        };
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
        if !python_only && !duckdb_extensions.is_empty() && (duckdb_changed || r_changed) {
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
            let selected = if let Some(selected) = early_python {
                selected
            } else {
                if !resolver.has_uv() {
                    self.ensure_startup(generation)
                        .map_err(EnvironmentResolutionFailure::Operation)?;
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
                    resolver
                        .set_resolved_uv(classify_resolver_result(result, stop_handle.as_ref())?);
                }
                self.resolve_managed_python_host(
                    generation,
                    candidate,
                    &resolver,
                    environment.r.as_ref(),
                    None,
                )?
            };
            if !environment.custom_worker {
                let inspected = self.inspect_managed_python(generation, &selected, &resolver)?;
                let runtime = environment
                    .local_runtime
                    .as_mut()
                    .expect("built-in environment retains runtime capabilities");
                if let Some(python) = &mut runtime.python {
                    *python.selected = inspected;
                } else {
                    runtime.python = Some(crate::local_runtime::Python {
                        selected: Box::new(inspected),
                        explicit: None,
                        managed: true,
                        duckdb_extension_directory: None,
                    });
                }
            }
            environment.python = Some(PythonEnvironment::Managed { selected, resolver });
        }
        if python_only && !duckdb_extensions.is_empty() && (duckdb_changed || python_changed) {
            self.resolve_python_duckdb_extensions_for_environment(
                generation,
                &environment,
                &duckdb_extensions.iter().cloned().collect::<Vec<_>>(),
            )?;
        }
        self.ensure_startup(generation)
            .map_err(EnvironmentResolutionFailure::Operation)?;
        environment.duckdb_extensions = duckdb_extensions;
        Ok(environment)
    }

    /// Resolve and inspect the complete candidate while the accepted running
    /// environment remains locked. The executable path is resolver input only;
    /// it never becomes a user declaration.
    pub(super) fn resolve_live_native_python(
        &self,
        generation: &WorkerGeneration,
        environment: &Environment,
        requirements: crate::worker_protocol::PythonRequirementManifest,
        extensions: &std::collections::BTreeSet<String>,
    ) -> Result<
        (crate::resolver::ManagedPython, crate::python::NativePython),
        EnvironmentResolutionFailure,
    > {
        let (current, resolver) = environment
            .python
            .as_ref()
            .ok_or_else(|| {
                EnvironmentResolutionFailure::Operation(
                    "managed Python environment is unavailable".into(),
                )
            })?
            .managed_parts()
            .map_err(EnvironmentResolutionFailure::Operation)?;
        let running = match environment
            .local_runtime
            .as_ref()
            .and_then(|runtime| runtime.python.as_ref())
        {
            Some(crate::local_runtime::Python { selected, .. }) => selected.as_ref().clone(),
            // R startup can initialize Python before Console's resolver hook
            // supplies a launch identity. Inspect the accepted executable on
            // its execution host; the worker also checks its actual live
            // library before mutating an adopted interpreter.
            None => self.inspect_managed_python(generation, current, resolver)?,
        };
        let candidate = self.resolve_managed_python_host(
            generation,
            requirements,
            resolver,
            environment.r.as_ref(),
            Some(current.python()),
        )?;
        let inspected = self.inspect_managed_python(generation, &candidate, resolver)?;
        crate::python::ensure_libpython_compatible(
            &inspected.embedding.libpython,
            &running.embedding.libpython,
        )
        .map_err(|error| EnvironmentResolutionFailure::Host(error.to_string()))?;

        if !extensions.is_empty()
            && environment
                .local_runtime
                .as_ref()
                .and_then(|runtime| runtime.duckdb_extension_directory())
                .is_some()
        {
            self.resolve_python_duckdb_extensions(
                generation,
                &candidate,
                resolver,
                &extensions.iter().cloned().collect::<Vec<_>>(),
                environment.local_runtime.as_ref(),
            )?;
        }
        Ok((candidate, inspected))
    }

    pub(super) fn inspect_managed_python(
        &self,
        generation: &WorkerGeneration,
        candidate: &crate::resolver::ManagedPython,
        resolver: &crate::resolver::execution::PythonConfiguration,
    ) -> Result<crate::python::NativePython, EnvironmentResolutionFailure> {
        self.ensure_startup(generation)
            .map_err(EnvironmentResolutionFailure::Operation)?;
        let mut stop_handle = None;
        let result =
            crate::resolver::execution::inspect_native(resolver, candidate.python(), |handle| {
                stop_handle = Some(handle.clone());
                self.register_resolver_stop_handle(generation, handle)
            });
        self.clear_resolver_stop_handle(generation)
            .map_err(EnvironmentResolutionFailure::Operation)?;
        self.ensure_startup(generation)
            .map_err(EnvironmentResolutionFailure::Operation)?;
        classify_resolver_result(result, stop_handle.as_ref())
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
            super::super::RResolver::Discover => {
                let existing = self
                    .0
                    .local_preparation
                    .lock()
                    .expect("local preparation lock")
                    .clone();
                let preparation = if let Some(existing) = existing {
                    existing
                } else {
                    let opened = crate::resolver::preparation::Preparation::open_local(
                        crate::resolver::preparation::Mode::Custom,
                        &|handle| self.register_resolver_stop_handle(generation, handle),
                    );
                    self.clear_resolver_stop_handle(generation)
                        .map_err(EnvironmentResolutionFailure::Operation)?;
                    let (preparation, _) =
                        opened.map_err(EnvironmentResolutionFailure::Operation)?;
                    *self
                        .0
                        .local_preparation
                        .lock()
                        .expect("local preparation lock") = Some(preparation.clone());
                    preparation
                };
                preparation.call(
                    crate::resolver::preparation::Operation::ResolveRStandalone { requirements },
                    on_started,
                )
            }
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
        selected_python: Option<&std::path::Path>,
    ) -> Result<crate::resolver::ManagedPython, EnvironmentResolutionFailure> {
        self.ensure_startup(generation)
            .map_err(EnvironmentResolutionFailure::Operation)?;
        let mut stop_handle = None;
        let result = crate::resolver::execution::resolve_python_manifest(
            requirements,
            resolver,
            managed_r,
            selected_python,
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
            let local = self
                .0
                .local_preparation
                .lock()
                .expect("local preparation lock")
                .clone();
            let preparation = local.as_ref().or_else(|| {
                self.0
                    .target
                    .as_ref()
                    .and_then(crate::target_session::Session::ssh_preparation)
            });
            let result = crate::resolver::execution::resolve_duckdb_extensions(
                preparation,
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

    pub(super) fn resolve_python_duckdb_extensions_for_environment(
        &self,
        generation: &WorkerGeneration,
        environment: &Environment,
        extensions: &[String],
    ) -> Result<(), EnvironmentResolutionFailure> {
        let (selected, resolver) = environment
            .python
            .as_ref()
            .ok_or_else(|| {
                EnvironmentResolutionFailure::Operation(
                    "managed Python environment is unavailable".to_string(),
                )
            })?
            .managed_parts()
            .map_err(EnvironmentResolutionFailure::Operation)?;
        self.resolve_python_duckdb_extensions(
            generation,
            selected,
            resolver,
            extensions,
            environment.local_runtime.as_ref(),
        )
    }

    fn resolve_python_duckdb_extensions(
        &self,
        generation: &WorkerGeneration,
        selected: &crate::resolver::ManagedPython,
        resolver: &crate::resolver::execution::PythonConfiguration,
        extensions: &[String],
        runtime: Option<&crate::local_runtime::Selection>,
    ) -> Result<(), EnvironmentResolutionFailure> {
        let extension_directory = runtime
            .and_then(crate::local_runtime::Selection::duckdb_extension_directory)
            .ok_or_else(|| {
                EnvironmentResolutionFailure::Operation(
                    "DuckDB extension preparation requires an absolute HOME at server startup"
                        .to_string(),
                )
            })?;
        self.ensure_startup(generation)
            .map_err(EnvironmentResolutionFailure::Operation)?;
        let mut stop_handle = None;
        let result = crate::resolver::execution::resolve_python_duckdb_extensions(
            resolver,
            selected,
            extensions,
            extension_directory,
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
}
