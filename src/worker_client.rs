use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};
use std::time::{Duration, Instant};

mod environment;
mod evaluation;
mod lifecycle;
mod output;

#[cfg(unix)]
mod child_exit;
#[cfg(unix)]
mod events;

#[cfg(unix)]
#[path = "worker_client/unix.rs"]
mod platform;

#[cfg(not(unix))]
#[path = "worker_client/unsupported.rs"]
mod platform;

use environment::{
    Environment, PreparationIntent, PrepareResult, PythonEnvironment, RuntimeRResolutionFailure,
};
pub(crate) use environment::{Requirements, RequirementsAction};
use evaluation::{Evaluation, EvaluationWait};
use lifecycle::{
    ControlledSendAdmission, LifecycleControl, OldGenerationCommitDisposition, WorkerGeneration,
};
pub(crate) use output::{Content, Response, ResponseDelivery};
use output::{OutputTape, SendFailure, SendResponse};

pub(crate) const DEFAULT_R_REQUIREMENTS: &[&str] = &[
    "tidyverse",
    "reticulate",
    "DBI",
    "duckdb",
    "arrow",
    "nanoarrow",
    "yyjsonr",
];

const DEFAULT_DUCKDB_EXTENSIONS: &[&str] = &["icu", "json", "sqlite"];

const CUSTOM_DUCKDB_R_REQUIREMENTS: &[&str] = &["DBI", "duckdb", "jsonlite"];
pub(crate) const WORKER_SHUTDOWN_GRACE: Duration = Duration::from_secs(1);
const INTERRUPT_GRACE: Duration = Duration::from_millis(100);

#[derive(Clone, Copy)]
pub(crate) enum SendControl {
    Interrupt,
    Restart,
}

pub(crate) struct SendRequest {
    pub(crate) cell: Option<crate::cell::Cell>,
    pub(crate) stdin: Option<String>,
    pub(crate) requirements: Option<Requirements>,
    pub(crate) control: Option<SendControl>,
    pub(crate) deadline: Instant,
    pub(crate) transcript: crate::transcript::Transcript,
    pub(crate) call_id: Option<u64>,
}

impl SendRequest {
    fn validate(&self, requirements_available: bool) -> Result<(), String> {
        let Some(requirements) = &self.requirements else {
            return Ok(());
        };
        if matches!(
            requirements.action,
            RequirementsAction::Set | RequirementsAction::Reset
        ) && matches!(self.control, Some(SendControl::Interrupt))
        {
            return Err("requirements.action=set/reset cannot accompany interrupt; use control=\"restart\" to replace a live environment".into());
        }
        if !requirements_available {
            return Err(
                "dynamic environment resolution is unavailable; install `ir` or `uv` and restart MCP Console"
                    .to_string(),
            );
        }
        if self.cell.is_none()
            && self.control.is_none()
            && self.stdin.as_ref().is_some_and(|stdin| !stdin.is_empty())
        {
            return Err(
                "requirements-only `send` performs standalone preparation and cannot also queue stdin"
                    .to_string(),
            );
        }
        if self.cell.is_none() && matches!(self.control, Some(SendControl::Interrupt)) {
            return Err(
                "`requirements` with `control = \"interrupt\"` requires a code cell".to_string(),
            );
        }
        // An interrupt and its stdin precede requirement-content errors. Validate
        // those only after the previous evaluation settles, before the new cell.
        if !matches!(self.control, Some(SendControl::Interrupt)) {
            requirements.validate()?;
        }
        Ok(())
    }
}

/// A cloneable handle to the single implicit session, including its startup.
#[derive(Clone)]
pub(crate) struct Client(Arc<ClientInner>);

struct ClientInner {
    configuration: OnceLock<ClientConfiguration>,
    startup: tokio::sync::watch::Sender<Option<Result<(), String>>>,
    /// The one evaluation occupying this session, independently of who is polling it.
    evaluation: Mutex<Option<ActiveEvaluation>>,
    /// Settles operations admitted before inline control reserves its optional new cell.
    admission: tokio::sync::RwLock<()>,
    preparation: tokio::sync::RwLock<()>,
    output: OutputTape,
    lifecycle: Mutex<LifecycleControl>,
    recording: Mutex<Option<crate::transcript::Transcript>>,
    startup_failed: AtomicBool,
    startup_stdin: Mutex<String>,
}

struct ClientConfiguration {
    runtime: platform::WorkerRuntime,
    program: PathBuf,
    arguments: Vec<OsString>,
    relay: Option<PathBuf>,
    no_sandbox: bool,
    sandbox_settings: crate::settings::SandboxSettings,
    worker: Mutex<WorkerState>,
    environment: Option<Mutex<Environment>>,
    requirements_snapshot: Mutex<serde_json::Value>,
    runtime_r_requirements: Vec<String>,
    dynamic_resolution: bool,
    python_only: bool,
    python_preparation: bool,
    local_preparation: Mutex<Option<crate::resolver::preparation::Preparation>>,
    target: Option<crate::target_session::Session>,
    /// A default worker may be replaced without discarding user runtime state.
    unused_default: AtomicBool,
}

impl std::ops::Deref for ClientInner {
    type Target = ClientConfiguration;

    fn deref(&self) -> &Self::Target {
        self.configuration
            .get()
            .expect("runtime configuration is ready")
    }
}

#[derive(Clone)]
enum RResolver {
    Discover,
    Pending(BuiltinSetup),
    Configured(crate::resolver::execution::RConfiguration),
    Disabled,
}

#[derive(Clone)]
struct BuiltinSetup {
    bootstrap: crate::resolver::execution::Bootstrap,
    python_resolver: crate::resolver::execution::PythonConfiguration,
    configured_python: Option<OsString>,
}

/// Describes one worker launch for the current runtime.
struct WorkerSpec<'a> {
    executable: &'a std::path::Path,
    arguments: &'a [OsString],
    relay: Option<&'a std::path::Path>,
    no_sandbox: bool,
    sandbox_settings: &'a crate::settings::SandboxSettings,
    python: Option<&'a PythonEnvironment>,
    managed_r: Option<&'a crate::resolver::ManagedR>,
    dynamic_resolution: bool,
    callbacks: WorkerCallbacks,
    local_runtime: Option<&'a crate::local_runtime::Selection>,
    target: Option<&'a crate::target_session::Session>,
}

struct IdleResponseSnapshot {
    cut: output::OutputCut,
    failure: Option<String>,
    input_requested: bool,
}

type RPreparationCommit =
    Box<dyn FnOnce(Result<(), String>) -> Result<PreparationOutcome, String> + Send + 'static>;

type PythonCandidate = (crate::resolver::ManagedPython, crate::python::NativePython);

type PythonPreparationCommit = Box<
    dyn FnOnce(Result<Option<PythonCandidate>, String>) -> Result<PreparationOutcome, String>
        + Send
        + 'static,
>;

enum PreparationOutcome {
    Completed(Result<(), String>),
    DiscardedByReplacement,
}

enum EnvironmentPreparationAdmissionFailure {
    Busy(String),
    Infrastructure(String),
}

#[derive(Clone, Copy)]
enum WorkerProcessOutcome {
    Exited(i32),
    Signaled(i32),
}

impl WorkerProcessOutcome {
    fn diagnostic(self) -> String {
        match self {
            Self::Exited(code) => format!("worker exited with status {code}"),
            Self::Signaled(signal) => format!("worker terminated by signal {signal}"),
        }
    }
}

struct WorkerRetirementFailure {
    message: String,
    outcome: Option<WorkerProcessOutcome>,
    can_replace: bool,
}

impl WorkerRetirementFailure {
    fn new(message: String, outcome: Option<WorkerProcessOutcome>) -> Self {
        Self {
            message,
            outcome,
            can_replace: false,
        }
    }

    fn attach_to(self, mut failure: SendFailure) -> SendFailure {
        failure.message.push_str(&format!(
            "; additionally failed to stop the worker: {}",
            self.message
        ));
        failure.worker_outcome(self.outcome)
    }
}

impl From<String> for WorkerRetirementFailure {
    fn from(message: String) -> Self {
        Self::new(message, None)
    }
}

#[derive(Clone)]
struct WorkerCallbacks {
    client: Client,
    generation: WorkerGeneration,
}

enum WorkerState {
    Initial,
    Stopped,
    Running(platform::Worker),
}

#[derive(Clone, Copy)]
enum WorkerRetirement {
    NeverStarted,
    AlreadyStopped,
    Stopped {
        outcome: Option<WorkerProcessOutcome>,
        failed: bool,
    },
}

impl WorkerState {
    fn stop_failed(&mut self) -> Result<WorkerRetirement, WorkerRetirementFailure> {
        match self {
            Self::Running(worker) => {
                let retirement = worker.shutdown_after_failure();
                let failed = worker.has_failure();
                *self = Self::Stopped;
                let outcome = retirement?;
                let failed =
                    failed.map_err(|message| WorkerRetirementFailure::new(message, outcome))?;
                Ok(WorkerRetirement::Stopped { outcome, failed })
            }
            Self::Initial => Ok(WorkerRetirement::NeverStarted),
            Self::Stopped => Ok(WorkerRetirement::AlreadyStopped),
        }
    }

    fn finish_retirement(&mut self) -> Result<WorkerRetirement, String> {
        match self {
            Self::Running(worker) => {
                let outcome = worker.finish_retirement()?;
                let failed = worker.has_failure()?;
                *self = Self::Stopped;
                Ok(WorkerRetirement::Stopped { outcome, failed })
            }
            Self::Initial => Ok(WorkerRetirement::NeverStarted),
            Self::Stopped => Ok(WorkerRetirement::AlreadyStopped),
        }
    }
}

#[derive(Clone)]
struct ActiveEvaluation {
    generation: WorkerGeneration,
    evaluation: Arc<Evaluation>,
    initial_requirements: Arc<Mutex<Option<Requirements>>>,
}

enum PreparedEvaluation {
    Started(Arc<Evaluation>, evaluation::WaitClaim),
    Failed(Response),
}

enum ControlledEvaluation {
    Started(Arc<Evaluation>, evaluation::WaitClaim),
    Returned(Response),
    Observe {
        evaluation: Arc<Evaluation>,
        wait_claim: evaluation::WaitClaim,
        cell_not_run: bool,
    },
}

enum PriorEvaluation {
    None,
    Completed(Response),
    Active(Arc<Evaluation>),
    Failed(Response),
}

enum ControlledStdinFailure {
    ActiveEvaluation(String),
    IdleWorker(SendFailure),
}

fn send_response_from_wait(wait: EvaluationWait) -> SendResponse {
    match wait {
        EvaluationWait::Running(output) => SendResponse::Running(output),
        EvaluationWait::InputRequested(output) => SendResponse::InputRequested(output),
        EvaluationWait::ReplacementStarting(output) => SendResponse::ReplacementStarting(output),
        EvaluationWait::ReplacementReady(output) => SendResponse::ReplacementReady(output),
        EvaluationWait::Completed(output) => SendResponse::Completed(output),
        EvaluationWait::Reclaimed(output) | EvaluationWait::Restarted(output) => {
            SendResponse::Restarted(output)
        }
    }
}

fn interrupted_cell_not_run_response(wait: EvaluationWait) -> Response {
    let mut response = send_response_from_wait(wait);
    match &mut response {
        SendResponse::Idle(output)
        | SendResponse::Failed(output)
        | SendResponse::Running(output)
        | SendResponse::InputRequested(output)
        | SendResponse::Completed(output)
        | SendResponse::ReplacementStarting(output)
        | SendResponse::ReplacementReady(output)
        | SendResponse::Restarted(output) => {
            output.push_tool_error("interrupted evaluation is still active; cell was not run");
        }
    }
    output::render_response(response)
}

impl Client {
    pub(crate) fn pending() -> Self {
        let (startup, _) = tokio::sync::watch::channel(None);
        Self(Arc::new(ClientInner {
            configuration: OnceLock::new(),
            startup,
            evaluation: Mutex::new(None),
            admission: tokio::sync::RwLock::new(()),
            preparation: tokio::sync::RwLock::new(()),
            output: OutputTape::new(),
            lifecycle: Mutex::new(LifecycleControl::new()),
            recording: Mutex::new(None),
            startup_failed: AtomicBool::new(false),
            startup_stdin: Mutex::new(String::new()),
        }))
    }

    pub(crate) fn configure(&self, configured: Self) {
        let inner = match Arc::try_unwrap(configured.0) {
            Ok(inner) => inner,
            Err(_) => panic!("runtime configuration must have one owner"),
        };
        assert!(
            self.0
                .configuration
                .set(inner.configuration.into_inner().expect("configured client"))
                .is_ok()
        );
        self.0.unused_default.store(
            self.0.environment.as_ref().is_some_and(|environment| {
                !environment
                    .lock()
                    .expect("worker environment lock")
                    .custom_worker
            }),
            Ordering::Release,
        );
    }

    pub(crate) fn finish_startup(&self, result: Result<(), String>) {
        self.0.startup.send_if_modified(|outcome| {
            if outcome.is_some() {
                return false;
            }
            *outcome = Some(result);
            true
        });
    }

    fn take_startup_failure(&self, generation: &WorkerGeneration) -> Result<bool, String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned")?;
        lifecycle.ensure_generation(generation)?;
        Ok(self.0.startup_failed.swap(false, Ordering::AcqRel))
    }

    pub(crate) async fn ready(&self) -> Result<(), String> {
        let mut result = self.0.startup.subscribe();
        let ready = result
            .wait_for(Option::is_some)
            .await
            .map_err(|_| "runtime startup stopped without a result".to_string())?;
        ready.as_ref().expect("startup completed").clone()
    }

    pub(crate) fn is_configured(&self) -> bool {
        self.0.configuration.get().is_some()
    }

    pub(crate) fn startup_finished(&self) -> bool {
        self.0.startup.borrow().is_some()
    }

    /// Launch the default process through the ordinary readiness/retirement path.
    /// Language runtimes retain their existing first-use initialization.
    pub(crate) fn prelaunch(&self, generation: &WorkerGeneration) {
        let result = (|| -> Result<(), SendFailure> {
            self.ensure_startup(generation)?;
            // An already admitted declaration selects the initial candidate through
            // the same transaction the evaluator uses after readiness.
            if let Some(active) = self.current_evaluation()? {
                let requirements = active
                    .initial_requirements
                    .lock()
                    .map_err(|_| "initial requirements lock poisoned".to_string())?
                    .take();
                if let Some(requirements) = requirements {
                    self.prepare_cell_requirements(requirements, generation)?;
                }
            }
            let _preparation = self.0.preparation.blocking_read();
            let mut worker = self
                .0
                .worker
                .lock()
                .map_err(|_| "worker lock poisoned".to_string())?;
            if let Err(mut failure) = self.start_worker(
                &mut worker,
                generation.clone(),
                false,
                |handle| self.register_stop_handle(generation, handle),
                || Ok(()),
            ) {
                if let Err(error) = self.clear_worker_stop_handle(generation) {
                    failure.message.push_str(&format!("; {error}"));
                }
                return Err(failure);
            }
            Ok(())
        })();
        if let Err(failure) = result {
            let lifecycle = self.0.lifecycle.lock().expect("worker lifecycle lock");
            if lifecycle.state == lifecycle::LifecycleState::Ready
                && lifecycle.generation.is(generation)
            {
                self.0.output.push_failure(failure);
                self.0.startup_failed.store(true, Ordering::Release);
            }
        }
    }

    pub(crate) fn new(
        program: PathBuf,
        relay: Option<PathBuf>,
        no_sandbox: bool,
        sandbox_settings: crate::settings::SandboxSettings,
    ) -> Result<Self, String> {
        Ok(Self::with_arguments(
            program,
            Vec::new(),
            relay,
            no_sandbox,
            sandbox_settings,
            Some(Environment {
                local_runtime: None,
                custom_worker: true,
                duckdb_extensions: Default::default(),
                duckdb_r_targets: Vec::new(),
                python: None,
                r: None,
                r_resolver: RResolver::Discover,
            }),
        ))
    }

    pub(crate) fn builtin(
        no_sandbox: bool,
        sandbox_settings: crate::settings::SandboxSettings,
        python: Option<PathBuf>,
        on_started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
        #[cfg(not(unix))]
        let python_resolver = crate::resolver::ManagedPythonResolverConfiguration::capture();
        let configured_python = python
            .map(PathBuf::into_os_string)
            .or_else(|| std::env::var_os("RETICULATE_PYTHON"));
        let program = std::env::current_exe()
            .map_err(|error| format!("failed to locate the R worker executable: {error}"))?;
        let local_runtime;
        let local_preparation;
        #[cfg(unix)]
        let (r, duckdb_extensions, python, r_resolver) =
            if !crate::local_runtime::Selection::r_is_present() {
                let (preparation, discovery) =
                    crate::resolver::preparation::Preparation::open_local(
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
                local_runtime = Some(selection);
                local_preparation = Some(preparation);
                let python = Some(match managed {
                    Some(selected) => PythonEnvironment::Managed { selected, resolver },
                    None => PythonEnvironment::bare(configured_python),
                });
                (None, extensions, python, RResolver::Disabled)
            } else {
                let (preparation, discovery) =
                    crate::resolver::preparation::Preparation::open_local(
                        crate::resolver::preparation::Mode::R,
                        on_started,
                    )?;
                use std::os::unix::ffi::OsStringExt;
                let home = PathBuf::from(OsString::from_vec(
                    discovery
                        .local_r_home_bytes
                        .ok_or("local R discovery has no R home")?,
                ));
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
                            python_resolver:
                                crate::resolver::execution::PythonConfiguration::Local {
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
        let mut client = Self::with_arguments(
            program,
            vec![OsString::from("worker")],
            None,
            no_sandbox,
            sandbox_settings,
            Some(Environment {
                local_runtime,
                custom_worker: false,
                duckdb_extensions,
                duckdb_r_targets: Vec::new(),
                python,
                r,
                r_resolver,
            }),
        );
        let inner = Arc::get_mut(&mut client.0)
            .expect("new client")
            .configuration
            .get_mut()
            .expect("configured client");
        inner.local_preparation = Mutex::new(local_preparation);
        Ok(client)
    }

    fn with_arguments(
        program: PathBuf,
        arguments: Vec<OsString>,
        relay: Option<PathBuf>,
        no_sandbox: bool,
        sandbox_settings: crate::settings::SandboxSettings,
        environment: Option<Environment>,
    ) -> Self {
        let dynamic_resolution = environment
            .as_ref()
            .is_some_and(|environment| !matches!(environment.r_resolver, RResolver::Disabled));
        let python_only = environment.as_ref().is_some_and(|environment| {
            environment
                .local_runtime
                .as_ref()
                .is_some_and(crate::local_runtime::Selection::python_only)
        });
        let python_preparation = python_only
            && environment.as_ref().is_some_and(|environment| {
                environment
                    .python
                    .as_ref()
                    .and_then(PythonEnvironment::managed)
                    .is_some()
            });
        let client = Self::pending();
        assert!(
            client
                .0
                .configuration
                .set(ClientConfiguration {
                    runtime: platform::WorkerRuntime,
                    program,
                    arguments,
                    relay,
                    no_sandbox,
                    sandbox_settings,
                    worker: Mutex::new(WorkerState::Initial),
                    requirements_snapshot: Mutex::new(
                        environment
                            .as_ref()
                            .map(Environment::inspection)
                            .unwrap_or(serde_json::Value::Null),
                    ),
                    runtime_r_requirements: environment
                        .as_ref()
                        .map(|env| {
                            env.runtime_r_requirements()
                                .iter()
                                .map(|s| (*s).into())
                                .collect()
                        })
                        .unwrap_or_default(),
                    environment: environment.map(Mutex::new),
                    dynamic_resolution,
                    python_only,
                    python_preparation,
                    local_preparation: Mutex::new(None),
                    target: None,
                    unused_default: AtomicBool::new(false),
                })
                .is_ok()
        );
        client.finish_startup(Ok(()));
        client
    }

    pub(crate) fn target(
        target: crate::settings::Target,
        roots: Vec<PathBuf>,
        no_sandbox: bool,
        policy: crate::settings::SandboxSettings,
        python: Option<PathBuf>,
        started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
        if matches!(target.compute, crate::settings::Compute::Host {}) {
            return Self::ssh(
                crate::ssh::Session::new(target, roots),
                no_sandbox,
                policy,
                python,
                started,
            );
        }
        let session = crate::target_session::Session::setup_compute(
            target,
            roots,
            &policy,
            no_sandbox,
            python.as_deref(),
            started,
        )?;
        let mut client = Self::with_arguments(
            std::env::current_exe().map_err(|error| error.to_string())?,
            Vec::new(),
            None,
            no_sandbox,
            policy,
            Some(Environment {
                local_runtime: None,
                custom_worker: false,
                duckdb_extensions: Default::default(),
                duckdb_r_targets: Vec::new(),
                python: Some(PythonEnvironment::bare(None)),
                r: None,
                r_resolver: RResolver::Disabled,
            }),
        );
        let inner = Arc::get_mut(&mut client.0)
            .expect("new client")
            .configuration
            .get_mut()
            .expect("configured client");
        inner.python_only = session.python_only();
        inner.target = Some(session);
        Ok(client)
    }

    pub(crate) fn record_with(&self, transcript: crate::transcript::Transcript) {
        *self.0.recording.lock().expect("recording lock") = Some(transcript);
    }

    pub(crate) fn target_metadata(&self) -> Option<serde_json::Value> {
        let target = self.0.target.as_ref()?;
        let mut metadata = target.metadata();
        let provider = target.provider();
        metadata["provider"] = serde_json::json!(provider);
        metadata["inner_native_runner"] = provider.needs_native_runner(self.0.no_sandbox).into();
        Some(metadata)
    }

    pub(crate) fn ssh(
        mut session: crate::ssh::Session,
        no_sandbox: bool,
        policy: crate::settings::SandboxSettings,
        configured_python: Option<PathBuf>,
        started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
        #[cfg(unix)]
        let (discovery, duckdb_extensions) = (|| {
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
        })()
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
        let discovery = session.discover(&policy, configured_python.as_deref(), started)?;
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
        let mut client = Self::with_arguments(
            std::env::current_exe().map_err(|error| error.to_string())?,
            Vec::new(),
            None,
            no_sandbox,
            policy,
            Some(Environment {
                local_runtime,
                custom_worker: false,
                duckdb_extensions,
                duckdb_r_targets: Vec::new(),
                python,
                r: None,
                r_resolver,
            }),
        );
        Arc::get_mut(&mut client.0)
            .expect("new client has one owner")
            .configuration
            .get_mut()
            .expect("configured client")
            .target = Some(crate::target_session::Session::Ssh(session));
        Ok(client)
    }

    pub(crate) fn python_available(&self) -> bool {
        self.0
            .target
            .as_ref()
            .is_none_or(|target| target.python_available())
    }

    pub(crate) fn python_only(&self) -> bool {
        self.0.python_only
    }

    pub(crate) fn python_preparation(&self) -> bool {
        self.0.python_preparation
    }

    pub(crate) fn dynamic_resolution(&self) -> bool {
        self.0.dynamic_resolution
    }

    /// Interprets preparation, control, evaluation, stdin, and polling for the session.
    pub(crate) async fn send(&self, request: SendRequest) -> Result<Response, String> {
        if self.is_configured()
            && let Some(cell) = &request.cell
        {
            self.validate_cell(cell)?;
        }
        // Admission does not depend on discovery or readiness. The established
        // evaluation slot owns an accepted early cell and all subsequent polls.
        if request.control.is_none()
            && (request.requirements.is_none()
                || (!self.startup_finished() && request.cell.is_some()))
        {
            request.validate(true)?;
            return Ok(
                match self
                    .send_inner(
                        request.cell,
                        request.stdin,
                        request.requirements,
                        request.deadline,
                        request.transcript,
                        request.call_id,
                    )
                    .await
                {
                    Ok(response) => output::render_response(response),
                    Err(failure) => output::direct_failure(failure.message),
                },
            );
        }
        if matches!(request.control, Some(SendControl::Interrupt))
            && request.cell.is_none()
            && request.requirements.is_none()
            && !self.startup_finished()
        {
            let startup_stdin = request
                .stdin
                .clone()
                .filter(|input| !input.is_empty())
                .map(|input| self.admit().map(|generation| (generation, input)))
                .transpose()?;
            let client = self.clone();
            tokio::task::spawn_blocking(move || client.interrupt_standalone_blocking())
                .await
                .map_err(|error| format!("startup interrupt task failed: {error}"))??;
            if let Some((generation, input)) = startup_stdin {
                #[cfg(unix)]
                self.queue_startup_stdin(&generation, input)?;
            }
            tokio::time::sleep(INTERRUPT_GRACE).await;
            if let Some(active) = self.current_evaluation()? {
                let claim = active.evaluation.claim()?;
                return Ok(output::render_response(send_response_from_wait(
                    active
                        .evaluation
                        .wait(
                            claim,
                            request.deadline.saturating_duration_since(Instant::now()),
                        )
                        .await?,
                )));
            }
            return Ok(output::render_response(SendResponse::ReplacementStarting(
                self.0.output.take(),
            )));
        }
        if !matches!(request.control, Some(SendControl::Restart)) || !self.is_configured() {
            if tokio::time::timeout(
                request.deadline.saturating_duration_since(Instant::now()),
                self.ready(),
            )
            .await
            .is_err()
            {
                let mut response = output::render_response(SendResponse::ReplacementStarting(
                    self.0.output.take(),
                ));
                if request.cell.is_some() {
                    response.push_tool_error(
                        "startup is pending; control was not applied and cell was not run",
                    );
                }
                return Ok(response);
            }
            self.ready().await?;
        }
        if let Some(requirements) = &request.requirements {
            self.validate_requirements(requirements)?;
        }
        if let Some(cell) = &request.cell {
            self.validate_cell(cell)?;
        }
        request.validate(self.dynamic_resolution() || self.python_preparation())?;
        if let Some(control) = request.control {
            return self.send_controlled(control, request).await;
        }
        let SendRequest {
            cell,
            stdin,
            requirements,
            control: _,
            deadline,
            transcript,
            call_id,
        } = request;
        if let Some(requirements) = requirements {
            if let Some(cell) = cell {
                return Ok(self
                    .send_with_requirements(
                        cell,
                        stdin,
                        requirements,
                        deadline,
                        transcript,
                        call_id,
                    )
                    .await);
            }
            let notice = match self.prepare(requirements).await? {
                PrepareResult::Prepared => "prepared",
                PrepareResult::RestartRequired => "restart required",
                PrepareResult::Failed(response) | PrepareResult::WorkerStopped(response) => {
                    return Ok(response);
                }
            };
            let mut response = Response::default();
            response.push_notice(notice);
            return Ok(response);
        }
        Ok(
            match self
                .send_inner(cell, stdin, None, deadline, transcript, call_id)
                .await
            {
                Ok(response) => output::render_response(response),
                Err(failure) => {
                    let mut response = Response::default();
                    response.push_failure(failure);
                    response
                }
            },
        )
    }

    async fn send_controlled(
        &self,
        control: SendControl,
        request: SendRequest,
    ) -> Result<Response, String> {
        let deadline = request.deadline;
        let direct_restart_error = matches!(control, SendControl::Restart)
            && request.requirements.is_some()
            && request.cell.is_none();
        let client = self.clone();
        let admission = tokio::task::spawn_blocking(move || {
            client.control_and_start_evaluation(control, request)
        })
        .await;
        let admission = match admission {
            Ok(Ok(admission)) => admission,
            Ok(Err(error)) if direct_restart_error => return Err(error),
            Ok(Err(error)) => return Ok(output::direct_failure(error)),
            Err(error) => {
                return Ok(output::direct_failure(format!(
                    "session control task failed: {error}"
                )));
            }
        };
        Ok(match admission {
            ControlledEvaluation::Started(evaluation, wait_claim) => {
                match evaluation
                    .wait(
                        wait_claim,
                        deadline.saturating_duration_since(Instant::now()),
                    )
                    .await
                {
                    Ok(wait) => output::render_response(send_response_from_wait(wait)),
                    Err(error) => output::direct_failure(error),
                }
            }
            ControlledEvaluation::Returned(response) => response,
            ControlledEvaluation::Observe {
                evaluation,
                wait_claim,
                cell_not_run,
            } => match evaluation
                .wait(
                    wait_claim,
                    if cell_not_run {
                        Duration::ZERO
                    } else {
                        deadline.saturating_duration_since(Instant::now())
                    },
                )
                .await
            {
                Ok(wait) if cell_not_run => interrupted_cell_not_run_response(wait),
                Ok(wait) => output::render_response(send_response_from_wait(wait)),
                Err(error) => output::direct_failure(error),
            },
        })
    }

    fn control_and_start_evaluation(
        &self,
        requested: SendControl,
        request: SendRequest,
    ) -> Result<ControlledEvaluation, String> {
        let SendRequest {
            cell,
            stdin,
            requirements,
            control: _,
            deadline: _,
            transcript,
            call_id,
        } = request;
        let standalone_interrupt = matches!(requested, SendControl::Interrupt)
            && cell.is_none()
            && requirements.is_none()
            && stdin.as_ref().is_none_or(String::is_empty);
        let control = match self.begin_controlled_send() {
            Ok(control) => control,
            Err(_) if standalone_interrupt => {
                // A controlled restart owns admission while its resolver is live.
                // Preserve resolver-first signaling for an empty interrupt.
                self.interrupt_standalone_blocking()?;
                std::thread::sleep(INTERRUPT_GRACE);
                let control = match self.begin_controlled_send() {
                    Ok(control) => control,
                    Err(_) => {
                        // The interrupted control retains output recovery ownership.
                        return Ok(ControlledEvaluation::Returned(output::render_response(
                            SendResponse::Running(Response::default()),
                        )));
                    }
                };
                let generation = control.generation();
                return self.continue_after_interrupt(
                    &control,
                    generation,
                    cell,
                    requirements,
                    transcript,
                    call_id,
                );
            }
            Err(error) => return Err(error),
        };
        match requested {
            SendControl::Interrupt => self.interrupt_and_start_evaluation(
                &control,
                cell,
                stdin,
                requirements,
                transcript,
                call_id,
            ),
            SendControl::Restart => self.restart_and_start_evaluation(
                &control,
                cell,
                stdin,
                requirements,
                transcript,
                call_id,
            ),
        }
    }

    fn return_controlled_response(&self, mut response: Response) -> ControlledEvaluation {
        response.recover_to(self.0.output.clone());
        ControlledEvaluation::Returned(response)
    }

    fn return_controlled_failure(
        &self,
        mut response: Response,
        failure: SendFailure,
    ) -> ControlledEvaluation {
        response.extend_logical_region(self.0.output.take());
        response.push_failure(failure);
        self.return_controlled_response(response)
    }

    fn observe_interrupted_evaluation(
        &self,
        evaluation: Arc<Evaluation>,
        cell_not_run: bool,
    ) -> ControlledEvaluation {
        match evaluation.claim() {
            Ok(wait_claim) => ControlledEvaluation::Observe {
                evaluation,
                wait_claim,
                cell_not_run,
            },
            Err(error) => {
                let mut response = Response::default();
                if cell_not_run {
                    response.push_tool_error(
                        "interrupted evaluation is still active; cell was not run",
                    );
                } else {
                    response.push_tool_error(error);
                }
                self.return_controlled_response(response)
            }
        }
    }

    fn interrupt_and_start_evaluation(
        &self,
        control: &ControlledSendAdmission,
        cell: Option<crate::cell::Cell>,
        stdin: Option<String>,
        requirements: Option<Requirements>,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<ControlledEvaluation, String> {
        let generation = control.generation();
        if self.python_preparation() && requirements.is_some() {
            return Err("Python requirements cannot accompany control: interrupt; prepare before first use or with control: restart".into());
        }
        self.interrupt_blocking()?;
        self.ensure_controlled_generation(control, &generation)?;
        match self.submit_controlled_stdin(stdin, &generation, control) {
            Ok(()) => {}
            Err(ControlledStdinFailure::ActiveEvaluation(error)) => return Err(error),
            Err(ControlledStdinFailure::IdleWorker(failure)) => {
                return Ok(self.return_controlled_failure(Response::default(), failure));
            }
        }
        std::thread::sleep(INTERRUPT_GRACE);
        self.continue_after_interrupt(control, generation, cell, requirements, transcript, call_id)
    }

    fn continue_after_interrupt(
        &self,
        control: &ControlledSendAdmission,
        generation: WorkerGeneration,
        cell: Option<crate::cell::Cell>,
        requirements: Option<Requirements>,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<ControlledEvaluation, String> {
        self.ensure_controlled_generation(control, &generation)?;

        // An interrupted preparation retains read admission until its resolver
        // settles. A code-free interrupt must still answer after the grace.
        let _operation = if cell.is_none() {
            match self.0.admission.try_write() {
                Ok(operation) => operation,
                Err(_) => {
                    // The preparation retains output recovery ownership.
                    return Ok(ControlledEvaluation::Returned(output::render_response(
                        SendResponse::Running(Response::default()),
                    )));
                }
            }
        } else {
            self.admit_controlled_operation()
        };
        let prior = self.prior_evaluation_after_interrupt(&generation, control, cell.is_some())?;
        if cell.is_none() {
            return match prior {
                PriorEvaluation::Active(evaluation) => {
                    Ok(self.observe_interrupted_evaluation(evaluation, false))
                }
                PriorEvaluation::Completed(response) => {
                    Ok(self.return_controlled_response(response))
                }
                PriorEvaluation::Failed(response) => Ok(self.return_controlled_response(response)),
                PriorEvaluation::None => match self.take_idle_response(&generation) {
                    Ok(response) => {
                        Ok(self.return_controlled_response(output::render_response(response)))
                    }
                    Err(failure) => {
                        Ok(self.return_controlled_failure(Response::default(), failure))
                    }
                },
            };
        }
        let mut control_prelude = match prior {
            PriorEvaluation::Active(evaluation) => {
                return Ok(self.observe_interrupted_evaluation(evaluation, true));
            }
            PriorEvaluation::Completed(response) => response,
            PriorEvaluation::None => Response::default(),
            PriorEvaluation::Failed(mut response) => {
                response.push_tool_error(
                    "interrupted evaluation could not be settled; cell was not run",
                );
                return Ok(self.return_controlled_response(response));
            }
        };
        if let Some(requirements) = requirements {
            if let Err(error) = requirements.validate() {
                control_prelude.push_tool_error(error);
                return Ok(self.return_controlled_response(control_prelude));
            }
            let preparation = match self.admit_preparation() {
                Ok(preparation) => preparation,
                Err(error) => {
                    control_prelude.push_tool_error(error);
                    return Ok(self.return_controlled_response(control_prelude));
                }
            };
            let prepared = self.prepare_admitted(
                requirements,
                &generation,
                &preparation,
                PreparationIntent::BeforeEvaluation,
            );
            drop(preparation);
            match prepared {
                Err(error) => {
                    control_prelude.push_tool_error(error);
                    return Ok(self.return_controlled_response(control_prelude));
                }
                Ok(PrepareResult::Prepared) => {}
                Ok(PrepareResult::RestartRequired) => {
                    control_prelude
                        .push_tool_error("requirements require session restart; cell was not run");
                    return Ok(self.return_controlled_response(control_prelude));
                }
                Ok(PrepareResult::Failed(response) | PrepareResult::WorkerStopped(response)) => {
                    control_prelude.extend_logical_region(response);
                    return Ok(self.return_controlled_response(control_prelude));
                }
            }
        }
        self.ensure_controlled_generation(control, &generation)?;
        let mut prelude = Some(control_prelude);
        let (evaluation, wait_claim) = match self.start_evaluation_admitted(
            cell.expect("controlled cell presence was checked"),
            None,
            generation,
            transcript,
            call_id,
            Some(control),
            &mut prelude,
            None,
        ) {
            Ok(started) => started,
            Err(error) => {
                let mut response = prelude.take().unwrap_or_default();
                response.push_tool_error(format!("{error}; cell was not run"));
                return Ok(self.return_controlled_response(response));
            }
        };
        Ok(ControlledEvaluation::Started(evaluation, wait_claim))
    }

    fn restart_and_start_evaluation(
        &self,
        control: &ControlledSendAdmission,
        cell: Option<crate::cell::Cell>,
        stdin: Option<String>,
        requirements: Option<Requirements>,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<ControlledEvaluation, String> {
        let requirements = requirements.unwrap_or_default();
        let stdin_follows = stdin.as_ref().is_some_and(|stdin| !stdin.is_empty());
        let restart = self.restart_blocking(
            requirements,
            WORKER_SHUTDOWN_GRACE,
            cell.is_some() || stdin_follows,
            Some(control),
        )?;
        let _operation = self.admit_controlled_operation();
        let Some(generation) = restart.generation else {
            let mut response = restart.response;
            if cell.is_some() {
                response.push_tool_error("session restart did not complete; cell was not run");
            }
            return Ok(self.return_controlled_response(response));
        };
        self.0.startup_failed.store(false, Ordering::Release);
        self.finish_startup(Ok(()));
        self.ensure_controlled_generation(control, &generation)?;
        let Some(cell) = cell else {
            let response = restart.response;
            if let Some(stdin) = stdin.filter(|stdin| !stdin.is_empty()) {
                if let Err(failure) = self.write_idle_stdin_blocking(stdin, generation.clone()) {
                    return Ok(self.return_controlled_failure(response, failure));
                }
                return Ok(self.return_controlled_response(output::render_response(
                    SendResponse::ReplacementReady(response),
                )));
            }
            return Ok(self.return_controlled_response(response));
        };
        let mut prelude = Some(restart.response);
        let (evaluation, wait_claim) = match self.start_evaluation_admitted(
            cell,
            stdin,
            generation,
            transcript,
            call_id,
            Some(control),
            &mut prelude,
            None,
        ) {
            Ok(started) => started,
            Err(error) => {
                let mut response = prelude.take().unwrap_or_default();
                response.push_tool_error(format!("{error}; cell was not run"));
                return Ok(self.return_controlled_response(response));
            }
        };
        Ok(ControlledEvaluation::Started(evaluation, wait_claim))
    }

    fn submit_controlled_stdin(
        &self,
        stdin: Option<String>,
        generation: &WorkerGeneration,
        control: &ControlledSendAdmission,
    ) -> Result<(), ControlledStdinFailure> {
        let Some(stdin) = stdin.filter(|stdin| !stdin.is_empty()) else {
            return Ok(());
        };
        self.ensure_controlled_generation(control, generation)
            .map_err(ControlledStdinFailure::ActiveEvaluation)?;
        if let Some(active) = self
            .current_evaluation()
            .map_err(ControlledStdinFailure::ActiveEvaluation)?
        {
            if !active.generation.is(generation) {
                return Err(ControlledStdinFailure::ActiveEvaluation(
                    "session restarted before stdin was queued".to_string(),
                ));
            }
            active
                .evaluation
                .submit_stdin(stdin)
                .map_err(ControlledStdinFailure::ActiveEvaluation)
        } else {
            self.write_idle_stdin_blocking(stdin, generation.clone())
                .map_err(ControlledStdinFailure::IdleWorker)
        }
    }

    fn prior_evaluation_after_interrupt(
        &self,
        generation: &WorkerGeneration,
        control: &ControlledSendAdmission,
        cell_follows: bool,
    ) -> Result<PriorEvaluation, String> {
        let mut active = self.evaluation()?;
        let Some(current) = active.as_ref() else {
            return Ok(PriorEvaluation::None);
        };
        self.ensure_controlled_generation(control, generation)?;
        if !current.generation.is(generation) {
            return Err("session restarted before the interrupted evaluation settled".to_string());
        }
        let evaluation = current.evaluation.clone();
        let reservation = if cell_follows {
            evaluation.reserve_completed_for_handoff()?
        } else {
            evaluation.reserve_completed_for_delivery()?
        };
        let Some(reservation) = reservation else {
            return Ok(PriorEvaluation::Active(evaluation));
        };
        *active = None;
        drop(active);
        match self.settle_reserved_evaluation(Some(reservation), false) {
            Ok(response) => Ok(PriorEvaluation::Completed(response)),
            Err(mut failure) => {
                failure.response.push_tool_error(failure.message);
                Ok(PriorEvaluation::Failed(failure.response))
            }
        }
    }

    async fn send_with_requirements(
        &self,
        cell: crate::cell::Cell,
        stdin: Option<String>,
        requirements: Requirements,
        deadline: Instant,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Response {
        let client = self.clone();
        let admission = tokio::task::spawn_blocking(move || {
            client.prepare_and_start_evaluation(cell, stdin, requirements, transcript, call_id)
        })
        .await;
        let admission = match admission {
            Ok(Ok(admission)) => admission,
            Ok(Err(error)) => return output::direct_failure(error),
            Err(error) => {
                return output::direct_failure(format!(
                    "requirement preparation task failed: {error}"
                ));
            }
        };
        let (evaluation, wait_claim) = match admission {
            PreparedEvaluation::Started(evaluation, wait_claim) => (evaluation, wait_claim),
            PreparedEvaluation::Failed(response) => return response,
        };
        let response = match evaluation
            .wait(
                wait_claim,
                deadline.saturating_duration_since(Instant::now()),
            )
            .await
        {
            Ok(wait) => send_response_from_wait(wait),
            Err(error) => return output::direct_failure(error),
        };
        output::render_response(response)
    }

    fn prepare_and_start_evaluation(
        &self,
        cell: crate::cell::Cell,
        stdin: Option<String>,
        requirements: Requirements,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<PreparedEvaluation, String> {
        let _operation = self.admit_operation()?;
        let generation = self.admit()?;
        let preparation = match self.admit_preparation() {
            Ok(preparation) => preparation,
            Err(error) => {
                let mut response = Response::default();
                response.push_tool_error(error);
                return Ok(PreparedEvaluation::Failed(response));
            }
        };
        let prepared = match self.prepare_admitted(
            requirements,
            &generation,
            &preparation,
            PreparationIntent::BeforeEvaluation,
        ) {
            Ok(prepared) => prepared,
            Err(error) => {
                let mut response = Response::default();
                response.push_tool_error(error);
                return Ok(PreparedEvaluation::Failed(response));
            }
        };
        let result = match prepared {
            PrepareResult::Prepared => {
                let (evaluation, wait_claim) =
                    self.start_evaluation(cell, stdin, generation, transcript, call_id)?;
                PreparedEvaluation::Started(evaluation, wait_claim)
            }
            PrepareResult::RestartRequired => {
                let mut response = self.0.output.take();
                response.push_tool_error("requirements require session restart; cell was not run");
                self.recover_failed_evaluation(response)
            }
            PrepareResult::Failed(response) | PrepareResult::WorkerStopped(response) => {
                self.recover_failed_evaluation(response)
            }
        };
        drop(preparation);
        Ok(result)
    }

    fn recover_failed_evaluation(&self, mut response: Response) -> PreparedEvaluation {
        response.recover_to(self.0.output.clone());
        PreparedEvaluation::Failed(response)
    }

    async fn send_inner(
        &self,
        cell: Option<crate::cell::Cell>,
        stdin: Option<String>,
        initial_requirements: Option<Requirements>,
        deadline: Instant,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<SendResponse, SendFailure> {
        let mut operation = Some(self.admit_operation()?);
        let generation = self.admit()?;
        let mut preparation = None;
        let (evaluation, wait_claim) = match cell {
            Some(cell) => {
                preparation = Some(self.admit_send()?);
                let mut prelude = None;
                self.start_evaluation_admitted(
                    cell,
                    stdin,
                    generation,
                    transcript,
                    call_id,
                    None,
                    &mut prelude,
                    initial_requirements,
                )?
            }
            None => match self.current_evaluation()? {
                Some(active) => {
                    // An accepted early cell may own preparation's write lock.
                    // Poll its evaluation without reserving another operation.
                    self.ensure_ordinary_generation(&generation)?;
                    if !active.generation.is(&generation) {
                        return Err("session restarted before the operation began"
                            .to_string()
                            .into());
                    }
                    let wait_claim = active.evaluation.claim()?;
                    if let Some(stdin) = stdin {
                        active.evaluation.submit_stdin(stdin)?;
                    }
                    (active.evaluation, wait_claim)
                }
                None => {
                    preparation = Some(self.admit_send()?);
                    self.ensure_ordinary_generation(&generation)?;
                    let mut startup = self.0.startup.subscribe();
                    let mut stdin = stdin;
                    #[cfg(unix)]
                    if !self.startup_finished()
                        && let Some(input) = stdin.take()
                    {
                        self.queue_startup_stdin(&generation, input)?;
                    }
                    // Waiting for shared startup is observation, not a
                    // reservation against its accepted cell's preparation.
                    if !self.startup_finished() {
                        drop(preparation.take());
                        drop(operation.take());
                    }
                    loop {
                        if let Some(active) = self.current_evaluation()? {
                            self.ensure_ordinary_generation(&generation)?;
                            if !active.generation.is(&generation) {
                                return Err("session restarted before the operation began"
                                    .to_string()
                                    .into());
                            }
                            let claim = active.evaluation.claim()?;
                            drop(preparation.take());
                            drop(operation.take());
                            return Ok(send_response_from_wait(
                                active
                                    .evaluation
                                    .wait(claim, deadline.saturating_duration_since(Instant::now()))
                                    .await?,
                            ));
                        }
                        if let Some(result) = startup.borrow_and_update().as_ref() {
                            if let Err(error) = result {
                                return Ok(SendResponse::Failed(Response::tool_error(
                                    error.clone(),
                                )));
                            }
                            break;
                        }
                        if tokio::time::timeout(
                            deadline.saturating_duration_since(Instant::now()),
                            startup.changed(),
                        )
                        .await
                        .is_err()
                        {
                            return Ok(SendResponse::ReplacementStarting(self.0.output.take()));
                        }
                    }
                    if let Some(stdin) = stdin
                        && let Err(failure) = self.write_idle_stdin(stdin, generation.clone()).await
                    {
                        match self.generation_status(&generation)? {
                            lifecycle::GenerationStatus::CurrentReady => {
                                let active = self.evaluation()?;
                                if active.is_some() {
                                    return Err(failure);
                                }
                                // A later cell must capture this failure in its
                                // idle prelude when it is admitted.
                                self.0.output.push_failure(failure);
                            }
                            lifecycle::GenerationStatus::CurrentClosing
                            | lifecycle::GenerationStatus::Changed => {
                                return Err(failure);
                            }
                        }
                    }
                    return self.take_idle_response(&generation);
                }
            },
        };
        drop(preparation);
        drop(operation);

        Ok(send_response_from_wait(
            evaluation
                .wait(
                    wait_claim,
                    deadline.saturating_duration_since(Instant::now()),
                )
                .await?,
        ))
    }

    fn start_evaluation(
        &self,
        cell: crate::cell::Cell,
        stdin: Option<String>,
        generation: WorkerGeneration,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<(Arc<Evaluation>, evaluation::WaitClaim), String> {
        let mut control_prelude = None;
        self.start_evaluation_admitted(
            cell,
            stdin,
            generation,
            transcript,
            call_id,
            None,
            &mut control_prelude,
            None,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn start_evaluation_admitted(
        &self,
        cell: crate::cell::Cell,
        stdin: Option<String>,
        generation: WorkerGeneration,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
        control: Option<&ControlledSendAdmission>,
        control_prelude: &mut Option<Response>,
        initial_requirements: Option<Requirements>,
    ) -> Result<(Arc<Evaluation>, evaluation::WaitClaim), String> {
        self.ensure_evaluation_admission(&generation, control)?;

        let mut active = self.evaluation()?;
        if let Some(active) = active.as_ref() {
            return Err(active.evaluation.reject_new_cell_message().to_string());
        }
        self.ensure_evaluation_admission(&generation, control)?;
        let startup = self.reserve_worker_startup(&generation)?;
        let idle_prelude = self.0.output.take_prelude();
        let evaluation = Arc::new(Evaluation::new(
            transcript,
            call_id,
            self.0.output.clone(),
            control_prelude.take().unwrap_or_default(),
            idle_prelude,
            control.is_some(),
        ));
        let wait_claim = evaluation
            .claim()
            .expect("a new evaluation must accept its first wait claim");
        if let Some(stdin) = stdin {
            evaluation
                .submit_stdin(stdin)
                .expect("a new evaluation must accept initial stdin");
        }
        *active = Some(ActiveEvaluation {
            generation: generation.clone(),
            evaluation: evaluation.clone(),
            initial_requirements: Arc::new(Mutex::new(initial_requirements)),
        });
        let initial_requirements = active.as_ref().unwrap().initial_requirements.clone();
        drop(active);

        let client = self.clone();
        let evaluator = evaluation.clone();
        let evaluation_task = tokio::task::spawn_blocking(move || {
            client.evaluate_blocking(cell, evaluator, generation, startup, initial_requirements);
        });
        let failed = evaluation.clone();
        let _completion_task = tokio::spawn(async move {
            if let Err(error) = evaluation_task.await {
                failed.complete_cell(Err(SendFailure::from(format!(
                    "worker task failed: {error}"
                ))));
            }
        });
        Ok((evaluation, wait_claim))
    }

    fn ensure_evaluation_admission(
        &self,
        generation: &WorkerGeneration,
        control: Option<&ControlledSendAdmission>,
    ) -> Result<(), String> {
        match control {
            Some(control) => self.ensure_controlled_generation(control, generation),
            None => self.ensure_ordinary_generation(generation),
        }
    }

    fn current_evaluation(&self) -> Result<Option<ActiveEvaluation>, String> {
        Ok(self.evaluation()?.clone())
    }

    fn evaluation(&self) -> Result<MutexGuard<'_, Option<ActiveEvaluation>>, String> {
        let mut active = self
            .0
            .evaluation
            .lock()
            .map_err(|_| "worker evaluation lock poisoned".to_string())?;
        let reap = active
            .as_ref()
            .map(|active| active.evaluation.reap_delivered_completion())
            .transpose()?
            .unwrap_or(false);
        if reap {
            *active = None;
        }
        Ok(active)
    }

    fn admit_send(&self) -> Result<tokio::sync::RwLockReadGuard<'_, ()>, String> {
        self.0
            .preparation
            .try_read()
            .map_err(|_| "session is preparing requirements".to_string())
    }

    fn admit_operation(&self) -> Result<tokio::sync::RwLockReadGuard<'_, ()>, String> {
        let operation = self
            .0
            .admission
            .try_read()
            .map_err(|_| "session control is in progress".to_string())?;
        self.admit()?;
        Ok(operation)
    }

    fn admit_controlled_operation(&self) -> tokio::sync::RwLockWriteGuard<'_, ()> {
        self.0.admission.blocking_write()
    }

    fn admit_preparation(&self) -> Result<tokio::sync::RwLockWriteGuard<'_, ()>, String> {
        match self.0.preparation.try_write() {
            Ok(preparation) => Ok(preparation),
            Err(_) if self.0.preparation.try_read().is_ok() => {
                Err("[requirements not prepared: worker is starting]".to_string())
            }
            Err(_) => Err("session is preparing requirements".to_string()),
        }
    }

    async fn write_idle_stdin(
        &self,
        stdin: String,
        generation: WorkerGeneration,
    ) -> Result<(), SendFailure> {
        if stdin.is_empty() {
            return Ok(());
        }
        let client = self.clone();
        tokio::task::spawn_blocking(move || client.write_idle_stdin_blocking(stdin, generation))
            .await
            .map_err(|error| SendFailure::from(format!("worker stdin task failed: {error}")))?
    }

    fn write_idle_stdin_blocking(
        &self,
        stdin: String,
        generation: WorkerGeneration,
    ) -> Result<(), SendFailure> {
        self.with_worker(&generation, |worker| {
            if !stdin.is_empty() {
                self.0.unused_default.store(false, Ordering::Release);
            }
            worker.write_stdin(stdin).map_err(SendFailure::from)
        })
    }

    /// Drains through an idle-response cut while this call owns the generation.
    fn take_idle_response(
        &self,
        generation: &WorkerGeneration,
    ) -> Result<SendResponse, SendFailure> {
        let evaluation = self.evaluation()?;
        if evaluation.is_some() {
            return Err("worker started evaluating before idle output was collected"
                .to_string()
                .into());
        }
        if self.take_startup_failure(generation)? {
            return Ok(SendResponse::Failed(self.0.output.take()));
        }
        drop(evaluation);
        self.ensure_generation(generation)?;

        let mut worker = self
            .0
            .worker
            .lock()
            .map_err(|_| "worker lock poisoned".to_string())?;
        self.ensure_generation(generation)?;
        let snapshot = match &mut *worker {
            WorkerState::Running(running) => running.idle_response_snapshot(&self.0.output)?,
            WorkerState::Initial | WorkerState::Stopped => IdleResponseSnapshot {
                cut: self.0.output.cut(),
                failure: None,
                input_requested: false,
            },
        };
        if let Some(message) = snapshot.failure {
            let mut failure = SendFailure::from(message);
            match self.stop_failed_worker(&mut worker, generation) {
                Ok(lifecycle::FailedWorkerStop::Stopped(outcome)) => {
                    failure = failure.worker_outcome(outcome).worker_stopped();
                }
                Ok(lifecycle::FailedWorkerStop::RestartOwnsWorker) => {}
                Err(stop_error) => {
                    failure = stop_error.attach_to(failure);
                }
            }
            self.0.output.push_failure(failure);
            return Ok(SendResponse::Failed(self.0.output.take()));
        }
        drop(worker);
        let output = self.0.output.drain_through(snapshot.cut);
        Ok(if snapshot.input_requested {
            SendResponse::InputRequested(output)
        } else {
            SendResponse::Idle(output)
        })
    }

    fn evaluate_blocking(
        &self,
        cell: crate::cell::Cell,
        evaluation: Arc<Evaluation>,
        generation: WorkerGeneration,
        startup: Option<Arc<lifecycle::WorkerStartupAdmission>>,
        initial_requirements: Arc<Mutex<Option<Requirements>>>,
    ) {
        let result = (|| {
            let readiness = tokio::runtime::Handle::current().block_on(self.ready());
            self.ensure_generation(&generation)
                .map_err(SendFailure::from)?;
            if !evaluation.is_interruptible()? {
                return Ok(());
            }
            if let Err(error) = readiness.and_then(|()| self.validate_cell(&cell)) {
                Response::tool_error(error).recover_to(self.0.output.clone());
                evaluation.complete_cell(Ok(()));
                return Ok(());
            }
            if self.take_startup_failure(&generation)? {
                evaluation.complete_cell(Ok(()));
                return Ok(());
            }
            if let Some(requirements) = initial_requirements
                .lock()
                .map_err(|_| "initial requirements lock poisoned".to_string())?
                .take()
            {
                self.prepare_cell_requirements(requirements, &generation)?;
            }
            let mut worker = self
                .0
                .worker
                .lock()
                .map_err(|_| SendFailure::from("worker lock poisoned".to_string()))?;
            let result = self.evaluate_with_worker(&mut worker, cell, &evaluation, generation);
            drop(startup);
            // Restart waits for this lock before taking the old output cut.
            // Publish failures before releasing it, including startup failures.
            if let Err(failure) = result {
                evaluation.complete_cell(Err(failure));
            }
            Ok(())
        })();
        if let Err(failure) = result {
            evaluation.complete_cell(Err(failure));
        }
    }

    fn validate_cell(&self, cell: &crate::cell::Cell) -> Result<(), String> {
        if self.python_only() && matches!(cell.language, crate::cell::Language::R) {
            return Err("R cells are unavailable in Python sessions without R".into());
        }
        if !self.python_available() && matches!(cell.language, crate::cell::Language::Python) {
            return Err("Python cells are unavailable: the target has no Python runtime".into());
        }
        Ok(())
    }

    fn prepare_cell_requirements(
        &self,
        requirements: Requirements,
        generation: &WorkerGeneration,
    ) -> Result<(), SendFailure> {
        self.validate_requirements(&requirements)?;
        // This accepted cell owns its preparation. Admission's short read
        // reservation must settle before its background owner can begin.
        let preparation = self.0.preparation.blocking_write();
        match self.prepare_admitted(
            requirements,
            generation,
            &preparation,
            PreparationIntent::StartupEvaluation,
        )? {
            PrepareResult::Prepared => Ok(()),
            PrepareResult::RestartRequired => {
                Err("requirements require session restart; cell was not run"
                    .to_string()
                    .into())
            }
            PrepareResult::Failed(mut response) | PrepareResult::WorkerStopped(mut response) => {
                response.recover_to(self.0.output.clone());
                Err("requirements were not prepared; cell was not run"
                    .to_string()
                    .into())
            }
        }
    }

    fn validate_requirements(&self, requirements: &Requirements) -> Result<(), String> {
        if let Some(target) = &self.0.target
            && !target.is_ssh()
        {
            let source = if matches!(target, crate::target_session::Session::Docker(..)) {
                "image"
            } else {
                "template"
            };
            return Err(format!(
                "dynamic environment resolution is disabled for {} targets; install packages in the {source} and start a new server session",
                target.protocol().0
            ));
        }
        if self.python_only() {
            if !self.python_preparation() {
                if !requirements.duckdb.is_empty() {
                    return Err("DuckDB extension preparation is unavailable with a user-selected Python environment; install extensions before starting the session".into());
                }
                return Err(crate::local_runtime::PREPARATION_DISABLED.into());
            }
            if !requirements.r.is_empty() {
                return Err("R requirements are unavailable in Python sessions without R".into());
            }
        }
        if !self.dynamic_resolution() && !self.python_preparation() {
            return Err("dynamic environment resolution is unavailable; install `ir` or `uv` and restart MCP Console".into());
        }
        Ok(())
    }

    fn evaluate_with_worker(
        &self,
        worker: &mut WorkerState,
        cell: crate::cell::Cell,
        evaluation: &Arc<Evaluation>,
        generation: WorkerGeneration,
    ) -> Result<(), SendFailure> {
        self.ensure_generation(&generation)
            .map_err(SendFailure::from)?;
        // Only an established worker can publish idle output in the admission
        // gap. A new worker's startup output remains part of this call.
        let capture_idle_prelude = matches!(worker, WorkerState::Running(_));
        if let Err(mut failure) = self.start_worker(
            worker,
            generation.clone(),
            true,
            |stop_handle| self.register_stop_handle(&generation, stop_handle),
            || Ok(()),
        ) {
            if let Err(clear_error) = self.clear_worker_stop_handle(&generation) {
                failure.message.push_str(&format!(
                    "; additionally failed to clear the worker shutdown handle: {clear_error}"
                ));
            }
            return Err(failure);
        }
        let WorkerState::Running(running) = worker else {
            return Err(SendFailure::from("worker is not running".to_string()));
        };
        self.0.unused_default.store(false, Ordering::Release);
        let result = running
            .evaluate(cell, evaluation.clone(), capture_idle_prelude)
            .map_err(|message| evaluation.classify_failure(message));
        let mut failure = match result {
            Ok(()) => return Ok(()),
            Err(failure) => failure,
        };
        match self.stop_failed_worker(worker, &generation) {
            Ok(lifecycle::FailedWorkerStop::Stopped(outcome)) => {
                failure = failure.worker_outcome(outcome);
            }
            Ok(lifecycle::FailedWorkerStop::RestartOwnsWorker) => return Err(failure),
            Err(stop_error) => {
                return Err(stop_error.attach_to(failure));
            }
        }

        let replacement_startup = self.0.preparation.blocking_read();
        evaluation.start_replacement(failure.worker_stopped());
        let replacement = self
            .start_worker(
                worker,
                generation.clone(),
                true,
                |stop_handle| self.register_stop_handle(&generation, stop_handle),
                || Ok(()),
            )
            .map_err(|mut failure| {
                if let Err(clear_error) = self.clear_worker_stop_handle(&generation) {
                    failure.message.push_str(&format!(
                        "; additionally failed to clear the worker shutdown handle: {clear_error}"
                    ));
                }
                failure
            });
        // A delivered replacement result must admit the next preparation.
        drop(replacement_startup);
        evaluation.finish_replacement(replacement);
        Ok(())
    }

    fn with_worker<T>(
        &self,
        generation: &WorkerGeneration,
        operation: impl FnOnce(&mut platform::Worker) -> Result<T, SendFailure>,
    ) -> Result<T, SendFailure> {
        self.ensure_generation(generation)
            .map_err(SendFailure::from)?;

        let mut worker = self
            .0
            .worker
            .lock()
            .map_err(|_| SendFailure::from("worker lock poisoned".to_string()))?;
        self.ensure_generation(generation)
            .map_err(SendFailure::from)?;

        if let Err(mut failure) = self.start_worker(
            &mut worker,
            generation.clone(),
            true,
            |stop_handle| self.register_stop_handle(generation, stop_handle),
            || Ok(()),
        ) {
            if let Err(clear_error) = self.clear_worker_stop_handle(generation) {
                failure.message.push_str(&format!(
                    "; additionally failed to clear the worker shutdown handle: {clear_error}"
                ));
            }
            return Err(failure);
        }
        let WorkerState::Running(running) = &mut *worker else {
            unreachable!("worker should be running");
        };
        match operation(running) {
            Ok(result) => Ok(result),
            Err(failure) => match self.stop_failed_worker(&mut worker, generation) {
                Ok(lifecycle::FailedWorkerStop::Stopped(outcome)) => {
                    Err(failure.worker_outcome(outcome).worker_stopped())
                }
                Ok(lifecycle::FailedWorkerStop::RestartOwnsWorker) => Err(failure),
                Err(stop_error) => Err(stop_error.attach_to(failure)),
            },
        }
    }

    fn start_worker(
        &self,
        worker: &mut WorkerState,
        generation: WorkerGeneration,
        announce_replacement: bool,
        on_started: impl FnOnce(platform::WorkerShutdownHandle) -> Result<(), String>,
        on_ready: impl FnOnce() -> Result<(), String>,
    ) -> Result<(), SendFailure> {
        let replacing = matches!(&*worker, WorkerState::Stopped);
        if !matches!(&*worker, WorkerState::Running(_)) {
            #[cfg(unix)]
            if let Some(preparation) = &*self
                .0
                .local_preparation
                .lock()
                .map_err(|_| "local preparation lock poisoned".to_string())?
            {
                preparation.check_ready()?;
            }
            let _startup = self.reserve_worker_startup(&generation)?;
            let mut environment = match &self.0.environment {
                Some(environment) => Some(
                    environment
                        .lock()
                        .map_err(|_| "worker environment lock poisoned".to_string())?,
                ),
                None => None,
            };
            if let Some(environment) = environment.as_mut()
                && matches!(environment.r_resolver, RResolver::Pending(_))
            {
                let delta =
                    environment::RequirementDelta::calculate(environment, Requirements::default())?;
                let prepared = self
                    .resolve_prestart_environment(&generation, environment, delta)
                    .map_err(|failure| SendFailure::from(failure.into_message()))?;
                let lifecycle = self
                    .0
                    .lifecycle
                    .lock()
                    .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
                lifecycle.ensure_startup(&generation)?;
                **environment = prepared;
                self.publish_requirements(environment);
            }
            let python = environment
                .as_ref()
                .and_then(|environment| environment.python.as_ref());
            let managed_r = environment
                .as_ref()
                .and_then(|environment| environment.r.as_ref());
            let spec = WorkerSpec {
                target: self.0.target.as_ref(),
                local_runtime: environment
                    .as_ref()
                    .and_then(|environment| environment.local_runtime.as_ref()),
                executable: &self.0.program,
                arguments: &self.0.arguments,
                relay: self.0.relay.as_deref(),
                no_sandbox: self.0.no_sandbox,
                sandbox_settings: &self.0.sandbox_settings,
                python,
                managed_r,
                dynamic_resolution: self.dynamic_resolution(),
                callbacks: WorkerCallbacks {
                    client: self.clone(),
                    generation,
                },
            };
            if replacing && announce_replacement {
                self.0
                    .output
                    .push_notice_line(output::WORKER_STARTING_NOTICE);
            }
            let running =
                self.0
                    .runtime
                    .spawn(spec, self.0.output.clone(), on_started, on_ready)?;
            if let Some(environment) = environment.as_mut() {
                // An external `--worker` must apply its first managed R layer before
                // loading DuckDB; arbitrary preloaded namespaces are not tracked.
                environment.duckdb_r_targets = environment.r.iter().cloned().collect();
            }
            *worker = WorkerState::Running(running);
        }
        Ok(())
    }
}

impl WorkerCallbacks {
    fn resolve_r(
        &self,
        packages: Vec<String>,
    ) -> Result<crate::resolver::ManagedR, RuntimeRResolutionFailure> {
        self.client
            .resolve_runtime_r(self.generation.clone(), packages)
    }

    fn activate_r(
        &self,
        library: String,
        candidates: &mut Vec<crate::resolver::ManagedR>,
    ) -> Result<OldGenerationCommitDisposition, String> {
        self.client
            .activate_runtime_r(self.generation.clone(), library, candidates)
    }

    fn fail_r_activation(
        &self,
        library: String,
        candidates: &mut Vec<crate::resolver::ManagedR>,
    ) -> Result<OldGenerationCommitDisposition, String> {
        self.client
            .fail_runtime_r_activation(self.generation.clone(), library, candidates)
    }

    fn resolve_python(
        &self,
        request: crate::worker_protocol::PythonResolveRequest,
        duckdb_extensions: Option<std::collections::BTreeSet<String>>,
    ) -> Result<PythonCandidate, String> {
        self.client
            .resolve_runtime_python(self.generation.clone(), request, duckdb_extensions)
    }

    fn fail_python_activation(&self) -> Result<OldGenerationCommitDisposition, String> {
        self.client
            .require_restart_for_requirement_changes(&self.generation)
    }

    fn resolve_python_version(
        &self,
        request: crate::worker_protocol::PythonVersionResolveRequest,
    ) -> Result<String, String> {
        self.client
            .resolve_runtime_python_version(self.generation.clone(), request)
    }

    fn activate_python(
        &self,
        requirements: crate::worker_protocol::PythonRequirementManifest,
        candidate: Option<crate::resolver::ManagedPython>,
        configuration: Option<crate::python::NativePython>,
        duckdb_extensions: Option<std::collections::BTreeSet<String>>,
    ) -> Result<OldGenerationCommitDisposition, String> {
        self.client.activate_runtime_python(
            self.generation.clone(),
            requirements,
            candidate,
            configuration,
            duckdb_extensions,
        )
    }
}
