use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::atomic::AtomicBool;

use super::{Environment, PythonEnvironment, WorkerState, platform};

pub(crate) struct ClientConfiguration {
    pub(super) runtime: platform::WorkerRuntime,
    pub(super) program: PathBuf,
    pub(super) arguments: Vec<OsString>,
    pub(super) relay: Option<PathBuf>,
    pub(super) no_sandbox: bool,
    pub(super) sandbox_settings: crate::settings::SandboxSettings,
    pub(super) resolver_settings: crate::settings::SandboxSettings,
    pub(super) duckdb_extension_directory: Option<PathBuf>,
    pub(super) worker: Mutex<WorkerState>,
    pub(super) environment: Option<Mutex<Environment>>,
    pub(super) requirements_snapshot: Mutex<serde_json::Value>,
    pub(super) runtime_r_requirements: Vec<String>,
    pub(super) dynamic_resolution: bool,
    pub(super) python_only: bool,
    pub(super) python_preparation: bool,
    pub(super) local_preparation: Mutex<Option<crate::resolver::preparation::Preparation>>,
    pub(super) target: Option<crate::target_session::Session>,
    /// Local built-in workers retain the controller selection across generations.
    pub(super) languages: Option<crate::cell::Languages>,
    /// A default worker may be replaced without discarding user runtime state.
    pub(super) unused_default: AtomicBool,
}

#[derive(Clone)]
pub(super) enum RResolver {
    Discover,
    Pending(BuiltinSetup),
    Configured(crate::resolver::execution::RConfiguration),
    Disabled,
}

#[derive(Clone)]
pub(super) struct BuiltinSetup {
    pub(super) bootstrap: crate::resolver::execution::Bootstrap,
    pub(super) python_resolver: crate::resolver::execution::PythonConfiguration,
    pub(super) configured_python: Option<OsString>,
}

impl ClientConfiguration {
    pub(crate) fn with_resolver_settings(
        mut self,
        settings: crate::settings::SandboxSettings,
    ) -> Result<Self, String> {
        let host_policy = Default::default();
        self.duckdb_extension_directory =
            crate::resolver::cache::duckdb_extension_directory(if !self.no_sandbox {
                &settings
            } else {
                &host_policy
            })?;
        self.resolver_settings = settings;
        Ok(self)
    }

    pub(crate) fn new(
        program: PathBuf,
        relay: Option<PathBuf>,
        no_sandbox: bool,
        sandbox_settings: crate::settings::SandboxSettings,
    ) -> Self {
        Self::with_arguments(
            program,
            Vec::new(),
            relay,
            no_sandbox,
            sandbox_settings,
            Environment {
                local_runtime: None,
                custom_worker: true,
                duckdb_extensions: Default::default(),
                duckdb_r_targets: Vec::new(),
                python: None,
                r: None,
                r_resolver: RResolver::Discover,
            },
        )
    }

    pub(crate) fn builtin(
        no_sandbox: bool,
        sandbox_settings: crate::settings::SandboxSettings,
        python: Option<PathBuf>,
        resolver_settings: crate::settings::SandboxSettings,
        diagnostics: crate::process_output::Diagnostics,
        on_started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
        #[cfg(windows)]
        let _ = &diagnostics;
        let host_policy = Default::default();
        let duckdb_extension_directory =
            crate::resolver::cache::duckdb_extension_directory(if !no_sandbox {
                &resolver_settings
            } else {
                &host_policy
            })?;
        let languages = crate::cell::Languages::from_environment()?;
        let configured_python = python
            .map(PathBuf::into_os_string)
            .or_else(|| std::env::var_os("RETICULATE_PYTHON"));
        let program = std::env::current_exe()
            .map_err(|error| format!("failed to locate the R worker executable: {error}"))?;
        let local_runtime;
        #[cfg(any(unix, windows))]
        let local_preparation;
        #[cfg(any(unix, windows))]
        let (preparation, discovery) = crate::resolver::preparation::Preparation::open_local(
            crate::resolver::preparation::Mode::Auto,
            (!no_sandbox).then(|| resolver_settings.clone()),
            configured_python.as_deref(),
            diagnostics.clone(),
            on_started,
        )?;
        #[cfg(any(unix, windows))]
        let (r, duckdb_extensions, python, r_resolver) = if discovery.selections.r_home.is_none() {
            let resolver = crate::resolver::execution::PythonConfiguration::Local {
                preparation: preparation.clone(),
                has_uv: discovery
                    .local_has_uv
                    .ok_or("local Python discovery has no uv result")?,
            };
            let selected = crate::local_runtime::Selection::python(
                configured_python.clone(),
                &resolver,
                duckdb_extension_directory.clone(),
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
            local_runtime = Some(selection);
            local_preparation = Some(preparation);
            let python = Some(match managed {
                Some(selected) => PythonEnvironment::Managed { selected, resolver },
                None => PythonEnvironment::bare(configured_python.clone()),
            });
            (None, extensions, python, RResolver::Disabled)
        } else {
            #[cfg(unix)]
            use std::os::unix::ffi::OsStringExt;
            #[cfg(unix)]
            let home = PathBuf::from(OsString::from_vec(
                discovery
                    .local_r_home_bytes
                    .ok_or("local R discovery has no R home")?,
            ));
            #[cfg(windows)]
            let home = PathBuf::from(
                discovery
                    .selections
                    .r_home
                    .clone()
                    .ok_or("local R discovery has no R home")?,
            );
            local_runtime = Some(crate::local_runtime::Selection {
                r_home: Some(home),
                python: None,
            });
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
                        configured_python: configured_python.clone(),
                    }),
                )
            } else {
                (
                    None,
                    Default::default(),
                    Some(PythonEnvironment::bare(configured_python.clone())),
                    RResolver::Disabled,
                )
            }
        };
        // Preserve Windows' independent peer runtimes even in a bare R session. An
        // explicit Python selection is inspected by the same preparation owner.
        #[cfg(windows)]
        let mut local_runtime = local_runtime;
        #[cfg(windows)]
        if let Some(runtime) = &mut local_runtime
            && runtime.python.is_none()
        {
            let explicit = configured_python
                .filter(|value| !value.is_empty() && value != "managed")
                .or_else(|| {
                    matches!(r_resolver, RResolver::Disabled)
                        .then(|| {
                            crate::resolver::find_path_entry("python").map(PathBuf::into_os_string)
                        })
                        .flatten()
                });
            if let Some(explicit) = explicit {
                let preparation = local_preparation.as_ref().expect("local preparation");
                let selected = crate::python::explicit_executable(&explicit)
                    .and_then(|executable| {
                        preparation.call(
                            crate::resolver::preparation::Operation::InspectPython { executable },
                            on_started,
                        )
                    })
                    .map_err(|error| match preparation.close() {
                        Ok(()) => error,
                        Err(cleanup) => format!("{error}; {cleanup}"),
                    })?;
                runtime.python = Some(crate::local_runtime::Python {
                    selected: Box::new(selected),
                    explicit: Some(explicit),
                    managed: false,
                    duckdb_extension_directory: None,
                });
            }
        }
        let mut configuration = Self::with_arguments(
            program,
            vec![
                OsString::from("worker"),
                OsString::from("--bootstrap-runtimes"),
            ],
            None,
            no_sandbox,
            sandbox_settings,
            Environment {
                local_runtime,
                custom_worker: false,
                duckdb_extensions,
                duckdb_r_targets: Vec::new(),
                python,
                r,
                r_resolver,
            },
        );
        configuration.duckdb_extension_directory = duckdb_extension_directory;
        configuration.resolver_settings = resolver_settings;
        configuration.local_preparation = Mutex::new(local_preparation);
        configuration.languages = Some(languages);
        Ok(configuration)
    }

    fn with_arguments(
        program: PathBuf,
        arguments: Vec<OsString>,
        relay: Option<PathBuf>,
        no_sandbox: bool,
        sandbox_settings: crate::settings::SandboxSettings,
        environment: Environment,
    ) -> Self {
        let dynamic_resolution = !matches!(environment.r_resolver, RResolver::Disabled);
        let python_only = environment
            .local_runtime
            .as_ref()
            .is_some_and(crate::local_runtime::Selection::python_only);
        let python_preparation = python_only
            && environment
                .python
                .as_ref()
                .and_then(PythonEnvironment::managed)
                .is_some();
        Self {
            runtime: platform::WorkerRuntime,
            program,
            arguments,
            relay,
            no_sandbox,
            sandbox_settings,
            resolver_settings: Default::default(),
            duckdb_extension_directory: None,
            worker: Mutex::new(WorkerState::Initial),
            requirements_snapshot: Mutex::new(environment.inspection()),
            runtime_r_requirements: environment
                .runtime_r_requirements()
                .iter()
                .map(|s| (*s).into())
                .collect(),
            environment: Some(Mutex::new(environment)),
            dynamic_resolution,
            python_only,
            python_preparation,
            local_preparation: Mutex::new(None),
            target: None,
            languages: None,
            unused_default: AtomicBool::new(false),
        }
    }

    pub(crate) fn target(
        target: crate::settings::Target,
        roots: Vec<PathBuf>,
        no_sandbox: bool,
        policy: crate::settings::SandboxSettings,
        python: Option<PathBuf>,
        diagnostics: crate::process_output::Diagnostics,
        started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
        let languages = crate::cell::Languages::from_environment()?;
        if matches!(target.compute, crate::settings::Compute::Host {}) {
            return Self::ssh(
                crate::ssh::Session::new(target, roots, languages),
                no_sandbox,
                policy,
                python,
                diagnostics,
                started,
            );
        }
        let session = crate::target_session::Session::setup_compute(
            target,
            roots,
            languages,
            &policy,
            no_sandbox,
            python.as_deref(),
            diagnostics,
            started,
        )?;
        let mut configuration = Self::with_arguments(
            std::env::current_exe().map_err(|error| error.to_string())?,
            Vec::new(),
            None,
            no_sandbox,
            policy,
            Environment {
                local_runtime: None,
                custom_worker: false,
                duckdb_extensions: Default::default(),
                duckdb_r_targets: Vec::new(),
                python: Some(PythonEnvironment::bare(None)),
                r: None,
                r_resolver: RResolver::Disabled,
            },
        );
        configuration.python_only = session.python_only();
        configuration.target = Some(session);
        Ok(configuration)
    }

    pub(crate) fn target_metadata(&self) -> Option<serde_json::Value> {
        let target = self.target.as_ref()?;
        let mut metadata = target.metadata();
        let provider = target.provider();
        metadata["provider"] = serde_json::json!(provider);
        metadata["inner_native_runner"] = provider.needs_native_runner(self.no_sandbox).into();
        Some(metadata)
    }

    fn ssh(
        mut session: crate::ssh::Session,
        no_sandbox: bool,
        policy: crate::settings::SandboxSettings,
        configured_python: Option<PathBuf>,
        diagnostics: crate::process_output::Diagnostics,
        started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
        #[cfg(unix)]
        let (discovery, duckdb_extensions) = (|| {
            let discovery = session.discover(
                &policy,
                configured_python.as_deref(),
                diagnostics.clone(),
                started,
            )?;
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
        })()
        .map_err(|error| {
            // No installed configuration owns shutdown if discovery fails.
            if let Some(preparation) = &session.preparation
                && let Err(cleanup) = preparation.close()
            {
                return format!("{error}; {cleanup}");
            }
            error
        })?;
        #[cfg(not(unix))]
        let discovery = session.discover(
            &policy,
            configured_python.as_deref(),
            diagnostics.clone(),
            started,
        )?;
        #[cfg(not(unix))]
        let duckdb_extensions = Default::default();
        let preparation = session
            .preparation
            .as_ref()
            .expect("remote discovery opened preparation")
            .clone();
        let r_selection =
            discovery
                .selections
                .r_home
                .as_ref()
                .map(|home| crate::local_runtime::Selection {
                    r_home: Some(PathBuf::from(home)),
                    python: None,
                });
        let selected_python = discovery.selections.python.map(OsString::from);
        let (r_resolver, python, local_runtime) = if let Some(native) = discovery.native {
            let resolver =
                crate::resolver::execution::PythonConfiguration::Ssh(preparation.clone());
            let python = match native.python {
                Some(selected) => PythonEnvironment::Managed { selected, resolver },
                None => PythonEnvironment::bare(selected_python),
            };
            (RResolver::Disabled, Some(python), Some(native.selection))
        } else if discovery.managed {
            (
                RResolver::Pending(BuiltinSetup {
                    bootstrap: crate::resolver::execution::Bootstrap::Ssh(preparation.clone()),
                    python_resolver: crate::resolver::execution::PythonConfiguration::Ssh(
                        preparation,
                    ),
                    configured_python: selected_python,
                }),
                None,
                r_selection,
            )
        } else {
            (
                RResolver::Disabled,
                Some(PythonEnvironment::bare(selected_python)),
                r_selection,
            )
        };
        let mut configuration = Self::with_arguments(
            std::env::current_exe().map_err(|error| error.to_string())?,
            Vec::new(),
            None,
            no_sandbox,
            policy,
            Environment {
                local_runtime,
                custom_worker: false,
                duckdb_extensions,
                duckdb_r_targets: Vec::new(),
                python,
                r: None,
                r_resolver,
            },
        );
        configuration.target = Some(crate::target_session::Session::Ssh(session));
        Ok(configuration)
    }

    pub(super) fn python_available(&self) -> bool {
        self.target
            .as_ref()
            .is_none_or(|target| target.python_available())
    }

    pub(crate) fn python_only(&self) -> bool {
        self.python_only
    }

    pub(crate) fn python_preparation(&self) -> bool {
        self.python_preparation
    }

    pub(crate) fn dynamic_resolution(&self) -> bool {
        self.dynamic_resolution
    }
}
