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
    pub(super) startup_source: Option<crate::settings::startup::Startup>,
    pub(super) startup_permitted: AtomicBool,
    pub(super) resolver_settings: crate::settings::SandboxSettings,
    pub(super) duckdb_extension_directory: Option<PathBuf>,
    pub(super) worker: Mutex<WorkerState>,
    pub(super) environment: Option<Mutex<Environment>>,
    pub(super) requirements_snapshot: Mutex<serde_json::Value>,
    pub(super) runtime_r_requirements: Vec<String>,
    pub(super) dynamic_resolution: bool,
    pub(super) python_only: bool,
    pub(super) python_preparation: bool,
    pub(super) resolver_preparation: Mutex<Option<crate::resolver::preparation::Preparation>>,
    /// Local built-in workers retain the controller selection across generations.
    pub(super) languages: Option<crate::cell::Languages>,
    /// A default worker may be replaced without discarding user runtime state.
    pub(super) unused_default: AtomicBool,
}

#[derive(Clone)]
pub(super) enum RResolver {
    Discover,
    Pending(BuiltinSetup),
    Configured(crate::resolver::preparation::Preparation),
    Disabled,
}

#[derive(Clone)]
pub(super) struct BuiltinSetup {
    pub(super) bootstrap: crate::resolver::preparation::Preparation,
    pub(super) python_resolver: crate::resolver::execution::PythonConfiguration,
    pub(super) configured_python: Option<OsString>,
}

impl ClientConfiguration {
    pub(crate) fn with_startup(
        mut self,
        startup: Option<crate::settings::startup::Startup>,
    ) -> Self {
        self.startup_source = startup;
        self
    }

    pub(crate) fn with_resolver_settings(
        mut self,
        settings: crate::settings::SandboxSettings,
    ) -> Result<Self, String> {
        self.duckdb_extension_directory =
            crate::resolver::cache::duckdb_extension_directory(&settings)?;
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
                startup: None,
                python_source: None,
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
        python: Option<crate::settings::PythonChoice>,
        r_settings: crate::settings::R,
        resolver_settings: crate::settings::SandboxSettings,
        diagnostics: crate::process_output::Diagnostics,
        on_started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
        #[cfg(windows)]
        let _ = &diagnostics;
        let duckdb_extension_directory =
            crate::resolver::cache::duckdb_extension_directory(&resolver_settings)?;
        let languages = crate::cell::Languages::from_environment()?;
        let explicit_python_configuration = python.is_some();
        let choice = match python {
            Some(choice) => choice,
            None => crate::settings::PythonChoice::ambient()?,
        };
        #[cfg(windows)]
        let mut choice = choice;
        if explicit_python_configuration {
            choice.validate_environment(&resolver_settings)?;
        }
        let configured_python = choice
            .executable
            .clone()
            .map(PathBuf::into_os_string)
            .or_else(|| Some(OsString::from("managed")));
        let program = std::env::current_exe().map_err(|error| error.to_string())?;
        let installation = r_settings
            .executable
            .as_ref()
            .map(|executable| {
                crate::resolver::preparation::Preparation::inspect::<
                    crate::local_runtime::RInstallation,
                >(
                    crate::resolver::preparation::Operation::InspectR {
                        executable: executable.clone(),
                    },
                    sandbox_settings.clone(),
                    no_sandbox,
                    diagnostics.clone(),
                    on_started,
                )
            })
            .transpose()?;
        let mode = if r_settings.resolution.enabled() {
            crate::resolver::preparation::Mode::Auto
        } else {
            crate::resolver::preparation::Mode::AutoBareR
        };
        let (preparation, discovery) = crate::resolver::preparation::Preparation::open_local(
            mode,
            resolver_settings.clone(),
            no_sandbox,
            configured_python.as_deref(),
            installation.clone(),
            diagnostics.clone(),
            on_started,
        )?;
        let result = (|| {
            let home = discovery.selections.r_home.as_ref().map(PathBuf::from);
            #[cfg(unix)]
            let home = discovery
                .local_r_home_bytes
                .as_ref()
                .map(|bytes| {
                    use std::os::unix::ffi::OsStringExt;
                    PathBuf::from(OsString::from_vec(bytes.clone()))
                })
                .or(home);
            if home.is_none()
                && r_settings
                    .packages
                    .as_ref()
                    .is_some_and(|packages| !packages.is_empty())
            {
                return Err("r.packages: configured R requirements need an available R installation; configure r.executable".into());
            }
            if !discovery.managed
                && home.is_some()
                && r_settings.resolution.enabled()
                && r_settings
                    .packages
                    .as_ref()
                    .map_or(!r_settings.resolution.automatic(), |packages| {
                        !packages.is_empty()
                    })
            {
                return Err("r.packages: R preparation is unavailable; install ir or uv, or use preinstalled packages with resolution: disabled".into());
            }
            let resolver = crate::resolver::execution::PythonConfiguration {
                preparation: preparation.clone(),
                has_uv: discovery
                    .local_has_uv
                    .ok_or("local discovery has no uv result")?,
            };
            let without_r = home.is_none();
            let inspected_python = choice
                .executable
                .as_ref()
                .filter(|_| cfg!(windows) || without_r || choice.source != "RETICULATE_PYTHON")
                .map(|explicit| {
                    crate::resolver::preparation::Preparation::inspect(
                        crate::resolver::preparation::Operation::InspectPython {
                            executable: explicit.into(),
                        },
                        sandbox_settings.clone(),
                        no_sandbox,
                        diagnostics.clone(),
                        on_started,
                    )
                })
                .transpose()?;
            let mut runtime = crate::local_runtime::Selection {
                r_home: home,
                installation,
                r_settings: r_settings.clone(),
                python_resolution: choice
                    .managed
                    .as_ref()
                    .map(|options| options.resolution)
                    .unwrap_or(crate::settings::Resolution::Disabled),
                python: None,
            };
            let r_resolver = if discovery.managed && r_settings.resolution.enabled() {
                RResolver::Pending(BuiltinSetup {
                    bootstrap: preparation.clone(),
                    python_resolver: resolver.clone(),
                    configured_python: configured_python.clone(),
                })
            } else {
                RResolver::Disabled
            };
            let mut python = None;
            let mut extensions = std::collections::BTreeSet::new();
            let manifest = choice
                .managed
                .as_ref()
                .map(|options| options.manifest(without_r));
            // Capture the immutable configured baseline before eager preparation.
            let mut startup = super::environment::Declaration::default();
            if !without_r
                && r_settings.resolution.enabled()
                && !matches!(r_resolver, RResolver::Disabled)
            {
                startup.r = r_settings.packages.clone().unwrap_or_else(|| {
                    super::DEFAULT_R_REQUIREMENTS
                        .iter()
                        .map(|name| (*name).into())
                        .collect()
                });
                startup.duckdb = super::DEFAULT_DUCKDB_EXTENSIONS
                    .iter()
                    .map(|name| (*name).into())
                    .collect();
            }
            if let Some(manifest) = &manifest
                && (resolver.has_uv() || matches!(r_resolver, RResolver::Pending(_)))
            {
                startup.python = manifest.packages.clone();
                startup.python_version = manifest.python_version.clone();
                startup.exclude_newer = manifest.exclude_newer.clone();
            }
            if !without_r && cfg!(unix) && choice.source == "RETICULATE_PYTHON" {
                // Preserve reticulate's ambient selection and partial R startup.
                // Typed configuration still captures an inspected interpreter.
                python = Some(PythonEnvironment::bare(configured_python.clone()));
            } else if let Some(explicit) = &choice.executable {
                let selected = inspected_python.expect("explicit Python was inspected");
                runtime.python = Some(crate::local_runtime::Python {
                    selected: Box::new(selected),
                    explicit: Some(explicit.clone().into_os_string()),
                    managed: false,
                    duckdb_extension_directory: None,
                });
                python = Some(PythonEnvironment::bare(Some(
                    explicit.clone().into_os_string(),
                )));
            } else if resolver.has_uv()
                && (without_r || !matches!(r_resolver, RResolver::Pending(_)))
            {
                let managed = crate::resolver::execution::resolve_python_manifest(
                    manifest.clone().expect("managed choice"),
                    &resolver,
                    None,
                    None,
                    on_started,
                )?;
                let selected = crate::resolver::execution::inspect_native(
                    &resolver,
                    managed.python(),
                    on_started,
                )?;
                // uv evaluates markers for the selected interpreter. Installed
                // metadata determines whether its implicit SQL defaults apply.
                let has_duckdb = selected.duckdb;
                runtime.python = Some(crate::local_runtime::Python {
                    selected: Box::new(selected),
                    explicit: None,
                    managed: true,
                    duckdb_extension_directory: without_r
                        .then(|| duckdb_extension_directory.clone())
                        .flatten(),
                });
                if without_r && has_duckdb {
                    startup.duckdb = crate::local_runtime::DEFAULT_DUCKDB_EXTENSIONS
                        .iter()
                        .map(|name| (*name).into())
                        .collect();
                    extensions = runtime.prepare_default_duckdb_extensions(
                        Some(&managed),
                        &resolver,
                        on_started,
                    )?;
                }
                python = Some(PythonEnvironment::Managed {
                    selected: managed,
                    resolver,
                });
            } else if !matches!(r_resolver, RResolver::Pending(_))
                && (without_r || choice.source != "default managed")
            {
                if !without_r {
                    return Err(crate::local_runtime::RESOLUTION_UNAVAILABLE.into());
                }
                return Err("Python sessions without R require `uv` on PATH; set python in .agents/console/config.yaml to use an existing environment".into());
            }
            #[cfg(windows)]
            if runtime.python.is_none()
                && matches!(r_resolver, RResolver::Disabled)
                && choice.source == "default managed"
                && let Some(executable) = crate::resolver::find_path_entry("python")
            {
                let selected = crate::resolver::preparation::Preparation::inspect(
                    crate::resolver::preparation::Operation::InspectPython {
                        executable: executable.clone(),
                    },
                    sandbox_settings.clone(),
                    no_sandbox,
                    diagnostics.clone(),
                    on_started,
                )?;
                runtime.python = Some(crate::local_runtime::Python {
                    selected: Box::new(selected),
                    explicit: Some(executable.clone().into_os_string()),
                    managed: false,
                    duckdb_extension_directory: None,
                });
                runtime.python_resolution = crate::settings::Resolution::Disabled;
                python = Some(PythonEnvironment::bare(Some(executable.into_os_string())));
                startup.python.clear();
                choice.source = "PATH".into();
            }
            Ok(Environment {
                startup: Some(startup.normalized()),
                python_source: Some(choice.source),
                local_runtime: Some(runtime),
                custom_worker: false,
                duckdb_extensions: extensions,
                duckdb_r_targets: Vec::new(),
                python,
                r: None,
                r_resolver,
            })
        })();
        let environment = match result {
            Ok(environment) => environment,
            Err(error) => {
                preparation
                    .close()
                    .map_err(|close| format!("{error}; {close}"))?;
                return Err(error);
            }
        };
        let mut configuration = Self::with_arguments(
            program,
            vec![
                OsString::from("worker"),
                OsString::from("--bootstrap-runtimes"),
            ],
            None,
            no_sandbox,
            sandbox_settings,
            environment,
        );
        configuration.duckdb_extension_directory = duckdb_extension_directory;
        configuration.resolver_settings = resolver_settings;
        configuration.resolver_preparation = Mutex::new(Some(preparation));
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
        let python_preparation = environment.manages_python();
        Self {
            runtime: platform::WorkerRuntime,
            program,
            arguments,
            relay,
            no_sandbox,
            sandbox_settings,
            startup_source: None,
            startup_permitted: AtomicBool::new(true),
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
            resolver_preparation: Mutex::new(None),
            languages: None,
            unused_default: AtomicBool::new(false),
        }
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

    pub(crate) fn startup_declaration(&self) -> super::Declaration {
        self.environment
            .as_ref()
            .expect("captured environment")
            .lock()
            .expect("environment lock")
            .startup_declaration()
    }
}
