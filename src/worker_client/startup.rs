//! Session-owned discovery and automatic worker startup.
use super::{BuiltinSetup, Client, Environment, PythonEnvironment, RResolver};
use std::ffi::OsString;
use std::path::PathBuf;

#[derive(Clone)]
pub(super) enum Configuration {
    Local {
        python: Option<PathBuf>,
    },
    Target {
        target: Box<crate::settings::Target>,
        roots: Vec<PathBuf>,
        python: Option<PathBuf>,
    },
}

pub(super) struct Discovered {
    pub(super) r_home: Option<PathBuf>,
    pub(super) environment: Environment,
    pub(super) preparation: Option<crate::resolver::preparation::Preparation>,
    pub(super) target: Option<crate::target_session::Session>,
}

pub(super) fn local(
    python: Option<PathBuf>,
    on_started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
) -> Result<Discovered, String> {
    #[cfg(not(unix))]
    let python_resolver = crate::resolver::ManagedPythonResolverConfiguration::capture();
    let configured_python = python
        .map(PathBuf::into_os_string)
        .or_else(|| std::env::var_os("RETICULATE_PYTHON"));
    let local_runtime;
    let r_home;
    let local_preparation;
    #[cfg(unix)]
    let (r, duckdb_extensions, python, r_resolver) =
        if !crate::local_runtime::Selection::r_is_present() {
            let (preparation, discovery) = crate::resolver::preparation::Preparation::open_local(
                crate::resolver::preparation::Mode::PythonOnly,
                on_started,
            )?;
            let resolver = crate::resolver::execution::PythonConfiguration::Local {
                preparation: preparation.clone(),
                has_uv: discovery
                    .local_has_uv
                    .ok_or("local Python discovery has no uv result")?,
            };
            let selected = crate::local_runtime::Selection::python(
                configured_python.clone(),
                &resolver,
                on_started,
            )
            .and_then(|(selection, managed)| {
                let extensions = selection.prepare_default_duckdb_extensions(
                    managed.as_ref(),
                    &resolver,
                    on_started,
                )?;
                Ok((selection, managed, extensions))
            });
            let (selection, managed, extensions) = match selected {
                Ok(selection) => selection,
                Err(error) => {
                    preparation
                        .close()
                        .map_err(|cleanup| format!("{error}; {cleanup}"))?;
                    return Err(error);
                }
            };
            r_home = None;
            local_runtime = Some(selection);
            local_preparation = Some(preparation);
            let python = Some(match managed {
                Some(selected) => PythonEnvironment::Managed { selected, resolver },
                None => PythonEnvironment::bare(configured_python),
            });
            (None, extensions, python, RResolver::Disabled)
        } else {
            let (preparation, discovery) = crate::resolver::preparation::Preparation::open_local(
                crate::resolver::preparation::Mode::R,
                on_started,
            )?;
            use std::os::unix::ffi::OsStringExt;
            let home = PathBuf::from(OsString::from_vec(
                discovery
                    .local_r_home_bytes
                    .ok_or("local R discovery has no R home")?,
            ));
            r_home = Some(home);
            local_runtime = None;
            local_preparation = Some(preparation.clone());
            if discovery.managed {
                (
                    None,
                    Default::default(),
                    None,
                    RResolver::Pending(BuiltinSetup {
                        bootstrap: crate::resolver::execution::Bootstrap::Local(
                            preparation.clone(),
                        ),
                        python_resolver: crate::resolver::execution::PythonConfiguration::Local {
                            preparation,
                            has_uv: discovery
                                .local_has_uv
                                .ok_or("local R discovery has no uv result")?,
                        },
                        configured_python,
                    }),
                )
            } else {
                (
                    None,
                    Default::default(),
                    Some(PythonEnvironment::bare(configured_python)),
                    RResolver::Disabled,
                )
            }
        };
    #[cfg(not(unix))]
    let local_preparation = None;
    #[cfg(not(unix))]
    let (r, duckdb_extensions, python, r_resolver) = (
        Option::<crate::resolver::ManagedR>::None,
        Default::default(),
        Some(PythonEnvironment::builtin(
            configured_python,
            python_resolver,
            None,
            on_started,
        )?),
        RResolver::Discover,
    );
    Ok(Discovered {
        r_home,
        environment: Environment {
            local_runtime,
            custom_worker: false,
            duckdb_extensions,
            duckdb_r_targets: Vec::new(),
            python,
            r,
            r_resolver,
        },
        preparation: local_preparation,
        target: None,
    })
}

pub(super) fn ssh(
    mut session: crate::ssh::Session,
    policy: crate::settings::SandboxSettings,
    configured_python: Option<PathBuf>,
    on_started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
) -> Result<Discovered, String> {
    #[cfg(unix)]
    let (discovery, duckdb_extensions) =
        (|started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>| {
            let discovery = session.discover(&policy, configured_python.as_deref(), started)?;
            let extensions = if let Some(native) = &discovery.native {
                native.selection.prepare_default_duckdb_extensions(
                    native.python.as_ref(),
                    &crate::resolver::execution::PythonConfiguration::Ssh(
                        session
                            .preparation
                            .as_ref()
                            .expect("remote preparation")
                            .clone(),
                    ),
                    started,
                )?
            } else {
                Default::default()
            };
            Ok((discovery, extensions))
        })(on_started)
        .map_err(|error| {
            // No Client owns shutdown if startup fails after discovery.
            if let Some(preparation) = &session.preparation
                && let Err(cleanup) = preparation.close()
            {
                return format!("{error}; {cleanup}");
            }
            error
        })?;
    #[cfg(not(unix))]
    let discovery = session.discover(&policy, configured_python.as_deref(), &|_| Ok(()))?;
    #[cfg(not(unix))]
    let duckdb_extensions = Default::default();
    let preparation = session
        .preparation
        .as_ref()
        .expect("remote discovery opened preparation")
        .clone();
    let r_home = discovery.selections.r_home.as_ref().map(PathBuf::from);
    let selected_python = discovery.selections.python.map(OsString::from);
    let (r_resolver, python, local_runtime) = if let Some(native) = discovery.native {
        let resolver = crate::resolver::execution::PythonConfiguration::Ssh(preparation.clone());
        let python = match native.python {
            Some(selected) => PythonEnvironment::Managed { selected, resolver },
            None => PythonEnvironment::bare(selected_python),
        };
        (RResolver::Disabled, Some(python), Some(native.selection))
    } else if discovery.managed {
        (
            RResolver::Pending(BuiltinSetup {
                bootstrap: crate::resolver::execution::Bootstrap::Ssh(preparation.clone()),
                python_resolver: crate::resolver::execution::PythonConfiguration::Ssh(preparation),
                configured_python: selected_python,
            }),
            None,
            None,
        )
    } else {
        (
            RResolver::Disabled,
            Some(PythonEnvironment::bare(selected_python)),
            None,
        )
    };
    Ok(Discovered {
        r_home,
        environment: Environment {
            local_runtime,
            custom_worker: false,
            duckdb_extensions,
            duckdb_r_targets: Vec::new(),
            python,
            r: None,
            r_resolver,
        },
        preparation: None,
        target: Some(crate::target_session::Session::Ssh(session)),
    })
}

impl Client {
    /// Start exactly once, after recording and transport shutdown ownership exist.
    pub(crate) fn warmup(&self) -> Result<(), String> {
        if self.0.setup.is_none() {
            return Ok(());
        }
        let mut task = self
            .0
            .warmup
            .lock()
            .map_err(|_| "warmup task lock poisoned")?;
        assert!(task.is_none(), "warmup already started");
        let generation = self.admit()?;
        self.0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned")?
            .warming = true;
        let client = self.clone();
        *task = Some(tokio::task::spawn_blocking(move || {
            let result = (|| {
                let mut worker = client.0.worker.lock().map_err(|_| "worker lock poisoned")?;
                if let Err(failure) = client.start_worker(
                    &mut worker,
                    generation.clone(),
                    false,
                    |handle| client.register_stop_handle(&generation, handle),
                    || Ok(()),
                ) {
                    return Err(failure.message);
                }
                let super::WorkerState::Running(running) = &*worker else {
                    unreachable!()
                };
                let handle = running.shutdown_handle();
                drop(worker);
                handle.wait_initialization()
            })();
            if let Ok(mut lifecycle) = client.0.lifecycle.lock()
                && lifecycle.generation.is(&generation)
            {
                lifecycle.warming = false;
            }
            if let Err(error) = result {
                client.retain_startup_failure(&generation, &error);
            }
            client.0.discovery_changed.notify_waiters();
        }));
        Ok(())
    }

    pub(super) async fn wait_discovered(&self) -> Result<(), String> {
        loop {
            let changed = self.0.discovery_changed.notified();
            if self.0.capabilities.get().is_some() {
                return Ok(());
            }
            {
                let lifecycle = self
                    .0
                    .lifecycle
                    .lock()
                    .map_err(|_| "worker lifecycle lock poisoned")?;
                if let Some(error) = &lifecycle.startup_failure {
                    return Err(error.clone());
                }
                lifecycle.ensure_startup(&lifecycle.generation)?;
            }
            changed.await;
        }
    }

    // The caller owns the worker slot. No other launch can duplicate discovery.
    pub(super) fn discover(&self, generation: &super::WorkerGeneration) -> Result<(), String> {
        if self.0.capabilities.get().is_some() {
            return Ok(());
        }
        if let Some(error) = self.0.discovery_failure.get() {
            return Err(format!(
                "{error}; start a new server session to repeat execution-host discovery"
            ));
        }
        let handles = std::sync::Mutex::new(Vec::new());
        let started = |handle: crate::resolver::ResolverStopHandle| {
            handles
                .lock()
                .expect("discovery handles")
                .push(handle.clone());
            self.register_resolver_stop_handle(generation, handle)
        };
        let result = match self
            .0
            .setup
            .as_ref()
            .expect("built-in startup configuration")
        {
            Configuration::Local { python } => local(python.clone(), &started),
            Configuration::Target {
                target,
                roots,
                python,
            } => {
                if matches!(target.compute, crate::settings::Compute::Host {}) {
                    ssh(
                        crate::ssh::Session::new(target.as_ref().clone(), roots.clone()),
                        self.0.sandbox_settings.clone(),
                        python.clone(),
                        &started,
                    )
                } else {
                    crate::target_session::Session::setup_compute(
                        target.as_ref().clone(),
                        roots.clone(),
                        &self.0.sandbox_settings,
                        self.0.no_sandbox,
                        python.as_deref(),
                        &started,
                    )
                    .map(|target| Discovered {
                        r_home: None,
                        environment: Environment {
                            local_runtime: None,
                            custom_worker: false,
                            duckdb_extensions: Default::default(),
                            duckdb_r_targets: Vec::new(),
                            python: Some(PythonEnvironment::bare(None)),
                            r: None,
                            r_resolver: RResolver::Disabled,
                        },
                        preparation: None,
                        target: Some(target),
                    })
                }
            }
        };
        self.clear_resolver_stop_handle(generation)?;
        let discovered = result.inspect_err(|error| {
            // A confirmed failure can be retried by explicit restart. An
            // unconfirmed discovery/probe must never lose its retirement block.
            if handles
                .into_inner()
                .expect("discovery handles")
                .iter()
                .any(|handle| !handle.cleanup_confirmed())
            {
                let _ = self.0.discovery_failure.set(error.clone());
            }
            *self
                .0
                .requirements_snapshot
                .lock()
                .expect("requirements snapshot lock") =
                serde_json::json!({"requirements": null, "prepared": false, "status": "failed"});
        })?;
        if let Some(home) = discovered.r_home {
            self.0
                .discovered_r_home
                .set(home)
                .map_err(|_| "R home already discovered")?;
        }
        // Install resource ownership even when shutdown won the publication race.
        *self
            .0
            .local_preparation
            .lock()
            .map_err(|_| "local preparation lock poisoned")? = discovered.preparation;
        let python_only = discovered
            .target
            .as_ref()
            .is_some_and(|target| target.python_only())
            || discovered
                .environment
                .local_runtime
                .as_ref()
                .is_some_and(crate::local_runtime::Selection::python_only);
        if let Some(target) = discovered.target {
            self.0
                .target
                .set(target)
                .map_err(|_| "execution target already discovered")?;
        }
        let mut environment = self
            .0
            .environment
            .as_ref()
            .expect("built-in environment")
            .lock()
            .map_err(|_| "worker environment lock poisoned")?;
        *environment = discovered.environment;
        self.publish_requirements(&environment);
        self.0
            .capabilities
            .set(super::Capabilities {
                runtime_r_requirements: environment
                    .runtime_r_requirements()
                    .iter()
                    .map(|s| (*s).into())
                    .collect(),
                dynamic_resolution: !matches!(environment.r_resolver, RResolver::Disabled),
                python_only,
                python_preparation: python_only
                    && environment
                        .python
                        .as_ref()
                        .and_then(PythonEnvironment::managed)
                        .is_some(),
            })
            .map_err(|_| "runtime capabilities already discovered")?;
        drop(environment);
        self.0.discovery_changed.notify_waiters();
        if let Some(transcript) = self
            .0
            .recording
            .lock()
            .map_err(|_| "recording lock poisoned")?
            .as_ref()
        {
            transcript.environment_discovered(
                self.dynamic_resolution(),
                self.python_preparation(),
                self.target_metadata(),
            );
        }
        self.ensure_startup(generation)
    }
}
