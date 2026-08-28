use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

mod environment;
mod evaluation;
mod lifecycle;
mod output;

#[cfg(target_os = "macos")]
mod events;

#[cfg(target_os = "macos")]
#[path = "worker_client/macos.rs"]
mod platform;

#[cfg(not(target_os = "macos"))]
#[path = "worker_client/unsupported.rs"]
mod platform;

use environment::{Environment, PreparationIntent, PythonEnvironment, RuntimeRResolutionFailure};
pub(crate) use environment::{PrepareResult, Requirements};
use evaluation::{Evaluation, EvaluationWait};
use lifecycle::{
    ControlledSendAdmission, LifecycleControl, OldGenerationCommitDisposition, WorkerGeneration,
};
pub(crate) use output::{Content, Response, ResponseDelivery};
use output::{OutputTape, SendFailure, SendResponse};

pub(crate) const DEFAULT_R_REQUIREMENTS: &[&str] = &[
    "tidyverse",
    "github::rstudio/reticulate",
    "DBI",
    "duckdb",
    "arrow",
    "nanoarrow",
];

#[cfg(target_os = "macos")]
const DEFAULT_DUCKDB_EXTENSIONS: &[&str] = &["icu", "json"];

const CUSTOM_DUCKDB_R_REQUIREMENTS: &[&str] = &["DBI", "duckdb", "jsonlite"];
pub(crate) const WORKER_SHUTDOWN_GRACE: Duration = Duration::from_secs(1);
const INTERRUPT_GRACE: Duration = Duration::from_millis(100);

#[derive(Clone, Copy)]
pub(crate) enum SendControl {
    Interrupt,
    Restart,
}

pub(crate) enum RequirementSubmission {
    Valid(Requirements),
    Invalid(String),
}

pub(crate) struct SendRequest {
    pub(crate) cell: Option<crate::cell::Cell>,
    pub(crate) stdin: Option<String>,
    pub(crate) requirements: Option<RequirementSubmission>,
    pub(crate) control: Option<SendControl>,
    pub(crate) timeout: Duration,
    pub(crate) transcript: crate::transcript::Transcript,
    pub(crate) call_id: Option<u64>,
}

/// A cloneable handle to one lazily started worker.
#[derive(Clone)]
pub(crate) struct Client(Arc<ClientInner>);

struct ClientInner {
    runtime: platform::WorkerRuntime,
    program: PathBuf,
    arguments: Vec<OsString>,
    relay: Option<PathBuf>,
    worker: Mutex<WorkerState>,
    /// The one evaluation occupying this session, independently of who is polling it.
    evaluation: Mutex<Option<ActiveEvaluation>>,
    /// Settles operations admitted before inline control reserves its optional new cell.
    admission: tokio::sync::RwLock<()>,
    preparation: tokio::sync::RwLock<()>,
    output: OutputTape,
    lifecycle: Mutex<LifecycleControl>,
    environment: Option<Mutex<Environment>>,
    r_resolver: RResolver,
}

#[derive(Clone)]
enum RResolver {
    Discover,
    Configured(crate::resolver::ManagedRResolverConfiguration),
    Disabled,
}

/// Describes one worker launch for the current runtime.
struct WorkerSpec<'a> {
    executable: &'a std::path::Path,
    arguments: &'a [OsString],
    relay: Option<&'a std::path::Path>,
    python: Option<&'a PythonEnvironment>,
    managed_r: Option<&'a crate::resolver::ManagedR>,
    dynamic_resolution: bool,
    callbacks: WorkerCallbacks,
}

struct IdleResponseSnapshot {
    cut: output::OutputCut,
    failure: Option<String>,
    input_requested: bool,
}

type RPreparationCommit =
    Box<dyn FnOnce(Result<(), String>) -> Result<PreparationOutcome, String> + Send + 'static>;

type PythonPreparationCommit = Box<
    dyn FnOnce(
            Result<Option<crate::resolver::ManagedPython>, String>,
        ) -> Result<PreparationOutcome, String>
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
}

impl WorkerRetirementFailure {
    fn new(message: String, outcome: Option<WorkerProcessOutcome>) -> Self {
        Self { message, outcome }
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
    pub(crate) fn new(program: PathBuf, relay: Option<PathBuf>) -> Result<Self, String> {
        Ok(Self::with_arguments(
            program,
            Vec::new(),
            relay,
            Some(Environment {
                custom_worker: true,
                duckdb_extensions: Default::default(),
                duckdb_r_targets: Vec::new(),
                python: None,
                r: None,
            }),
            RResolver::Discover,
        ))
    }

    pub(crate) fn builtin() -> Result<Self, String> {
        let mut python_resolver = crate::resolver::ManagedPythonResolverConfiguration::capture();
        let configured_python = std::env::var_os("RETICULATE_PYTHON");
        let program = std::env::current_exe()
            .map_err(|error| format!("failed to locate the R worker executable: {error}"))?;
        #[cfg(target_os = "macos")]
        let (r, duckdb_extensions, python, r_resolver) = {
            let r_resolver =
                crate::resolver::discover_r_resolver(&mut python_resolver, |_| Ok(()))?;
            match r_resolver {
                Some(r_resolver) => {
                    let r = crate::resolver::resolve_r_with(
                        &r_resolver,
                        DEFAULT_R_REQUIREMENTS
                            .iter()
                            .map(|requirement| (*requirement).to_string())
                            .collect(),
                        |_| Ok(()),
                    )?;
                    if PythonEnvironment::uses_managed(configured_python.as_deref())
                        && !python_resolver.has_uv()
                    {
                        let uv = r_resolver.resolve_uv(&r, &python_resolver, |_| Ok(()))?;
                        python_resolver.set_resolved_uv(uv);
                    }
                    let duckdb_extensions = DEFAULT_DUCKDB_EXTENSIONS
                        .iter()
                        .map(|extension| (*extension).to_string())
                        .collect::<Vec<_>>();
                    crate::resolver::resolve_duckdb_extensions(&r, &duckdb_extensions, |_| Ok(()))?;
                    let python =
                        PythonEnvironment::builtin(configured_python, python_resolver, Some(&r))?;
                    (
                        Some(r),
                        duckdb_extensions.into_iter().collect(),
                        python,
                        RResolver::Configured(r_resolver),
                    )
                }
                None => (
                    None,
                    Default::default(),
                    PythonEnvironment::bare(configured_python),
                    RResolver::Disabled,
                ),
            }
        };
        #[cfg(not(target_os = "macos"))]
        let (r, duckdb_extensions, python, r_resolver) = (
            Option::<crate::resolver::ManagedR>::None,
            Default::default(),
            PythonEnvironment::builtin(configured_python, python_resolver, None)?,
            RResolver::Discover,
        );
        Ok(Self::with_arguments(
            program,
            vec![OsString::from("worker")],
            None,
            Some(Environment {
                custom_worker: false,
                duckdb_extensions,
                duckdb_r_targets: Vec::new(),
                python: Some(python),
                r,
            }),
            r_resolver,
        ))
    }

    fn with_arguments(
        program: PathBuf,
        arguments: Vec<OsString>,
        relay: Option<PathBuf>,
        environment: Option<Environment>,
        r_resolver: RResolver,
    ) -> Self {
        Self(Arc::new(ClientInner {
            runtime: platform::WorkerRuntime,
            program,
            arguments,
            relay,
            worker: Mutex::new(WorkerState::Initial),
            evaluation: Mutex::new(None),
            admission: tokio::sync::RwLock::new(()),
            preparation: tokio::sync::RwLock::new(()),
            output: OutputTape::new(),
            lifecycle: Mutex::new(LifecycleControl::new()),
            environment: environment.map(Mutex::new),
            r_resolver,
        }))
    }

    pub(crate) fn dynamic_resolution(&self) -> bool {
        !matches!(self.0.r_resolver, RResolver::Disabled)
    }

    /// Starts one cell, supplies stdin, or collects an idle response.
    pub(crate) async fn send(&self, request: SendRequest) -> Result<Response, String> {
        if let Some(control) = request.control {
            return self.send_controlled(control, request).await;
        }
        let SendRequest {
            cell,
            stdin,
            requirements,
            control: _,
            timeout,
            transcript,
            call_id,
        } = request;
        if let Some(requirements) = requirements {
            let Some(cell) = cell else {
                return Ok(output::direct_failure(
                    "`requirements` requires a code cell",
                ));
            };
            let requirements = match requirements {
                RequirementSubmission::Valid(requirements) => requirements,
                RequirementSubmission::Invalid(error) => {
                    return Ok(output::direct_failure(error));
                }
            };
            return Ok(self
                .send_with_requirements(cell, stdin, requirements, timeout, transcript, call_id)
                .await);
        }
        Ok(
            match self
                .send_inner(cell, stdin, timeout, transcript, call_id)
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
        let timeout = request.timeout;
        let direct_restart_error = matches!(control, SendControl::Restart)
            && request.requirements.is_some()
            && request.cell.is_none();
        if matches!(control, SendControl::Interrupt)
            && request.requirements.is_some()
            && request.cell.is_none()
        {
            return Ok(output::direct_failure(
                "`requirements` with `control = \"interrupt\"` requires a code cell",
            ));
        }
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
                match evaluation.wait(wait_claim, timeout).await {
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
                        timeout
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
            timeout: _,
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
        requirements: Option<RequirementSubmission>,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<ControlledEvaluation, String> {
        let generation = control.generation();
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
        requirements: Option<RequirementSubmission>,
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
            let requirements = match requirements {
                RequirementSubmission::Valid(requirements) => requirements,
                RequirementSubmission::Invalid(error) => {
                    control_prelude.push_tool_error(error);
                    return Ok(self.return_controlled_response(control_prelude));
                }
            };
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
        requirements: Option<RequirementSubmission>,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<ControlledEvaluation, String> {
        let requirements = match requirements {
            Some(RequirementSubmission::Valid(requirements)) => requirements,
            Some(RequirementSubmission::Invalid(error)) => return Err(error),
            None => Requirements {
                duckdb: Vec::new(),
                python: Vec::new(),
                r: Vec::new(),
            },
        };
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
        timeout: Duration,
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
        let response = match evaluation.wait(wait_claim, timeout).await {
            Ok(EvaluationWait::Running(output)) => SendResponse::Running(output),
            Ok(EvaluationWait::InputRequested(output)) => SendResponse::InputRequested(output),
            Ok(EvaluationWait::ReplacementStarting(output)) => {
                SendResponse::ReplacementStarting(output)
            }
            Ok(EvaluationWait::ReplacementReady(output)) => SendResponse::ReplacementReady(output),
            Ok(EvaluationWait::Completed(output)) => SendResponse::Completed(output),
            Ok(EvaluationWait::Reclaimed(output)) => SendResponse::Restarted(output),
            Ok(EvaluationWait::Restarted(output)) => SendResponse::Restarted(output),
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
        timeout: Duration,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<SendResponse, SendFailure> {
        let operation = self.admit_operation()?;
        let generation = self.admit()?;
        let preparation = self.admit_send()?;
        let (evaluation, wait_claim) = match cell {
            Some(cell) => self.start_evaluation(cell, stdin, generation, transcript, call_id)?,
            None => match self.current_evaluation()? {
                Some(active) => {
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
                    self.ensure_ordinary_generation(&generation)?;
                    if let Some(stdin) = stdin
                        && let Err(failure) = self.write_idle_stdin(stdin, generation.clone()).await
                    {
                        match self.generation_status(&generation)? {
                            lifecycle::GenerationStatus::CurrentReady => {
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

        match evaluation.wait(wait_claim, timeout).await? {
            EvaluationWait::Running(output) => Ok(SendResponse::Running(output)),
            EvaluationWait::InputRequested(output) => Ok(SendResponse::InputRequested(output)),
            EvaluationWait::ReplacementStarting(output) => {
                Ok(SendResponse::ReplacementStarting(output))
            }
            EvaluationWait::ReplacementReady(output) => Ok(SendResponse::ReplacementReady(output)),
            EvaluationWait::Completed(output) => Ok(SendResponse::Completed(output)),
            EvaluationWait::Reclaimed(output) => Ok(SendResponse::Restarted(output)),
            EvaluationWait::Restarted(output) => Ok(SendResponse::Restarted(output)),
        }
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
    ) -> Result<(Arc<Evaluation>, evaluation::WaitClaim), String> {
        self.ensure_evaluation_admission(&generation, control)?;

        let mut active = self.evaluation()?;
        if let Some(active) = active.as_ref() {
            return Err(active.evaluation.reject_new_cell_message().to_string());
        }
        self.ensure_evaluation_admission(&generation, control)?;
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
        });
        drop(active);

        let client = self.clone();
        let evaluator = evaluation.clone();
        let evaluation_task = tokio::task::spawn_blocking(move || {
            client.evaluate_blocking(cell, evaluator, generation);
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
    ) {
        let result = self.evaluate_with_worker(cell, &evaluation, generation);
        if let Err(failure) = result {
            evaluation.complete_cell(Err(failure));
        }
    }

    fn evaluate_with_worker(
        &self,
        cell: crate::cell::Cell,
        evaluation: &Arc<Evaluation>,
        generation: WorkerGeneration,
    ) -> Result<(), SendFailure> {
        self.ensure_generation(&generation)
            .map_err(SendFailure::from)?;
        let mut worker = self
            .0
            .worker
            .lock()
            .map_err(|_| SendFailure::from("worker lock poisoned".to_string()))?;
        self.ensure_generation(&generation)
            .map_err(SendFailure::from)?;
        // Only an established worker can publish idle output in the admission
        // gap. A new worker's startup output remains part of this call.
        let capture_idle_prelude = matches!(&*worker, WorkerState::Running(_));
        if let Err(mut failure) = self.start_worker(
            &mut worker,
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
        let WorkerState::Running(running) = &mut *worker else {
            return Err(SendFailure::from("worker is not running".to_string()));
        };
        let result = running
            .evaluate(cell, evaluation.clone(), capture_idle_prelude)
            .map_err(|message| evaluation.classify_failure(message));
        let mut failure = match result {
            Ok(()) => return Ok(()),
            Err(failure) => failure,
        };
        match self.stop_failed_worker(&mut worker, &generation) {
            Ok(lifecycle::FailedWorkerStop::Stopped(outcome)) => {
                failure = failure.worker_outcome(outcome);
            }
            Ok(lifecycle::FailedWorkerStop::RestartOwnsWorker) => return Err(failure),
            Err(stop_error) => {
                return Err(stop_error.attach_to(failure));
            }
        }

        let _replacement_startup = self.0.preparation.blocking_read();
        evaluation.start_replacement(failure.worker_stopped());
        let replacement = self
            .start_worker(
                &mut worker,
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
            let mut environment = match &self.0.environment {
                Some(environment) => Some(
                    environment
                        .lock()
                        .map_err(|_| "worker environment lock poisoned".to_string())?,
                ),
                None => None,
            };
            let python = environment
                .as_ref()
                .and_then(|environment| environment.python.as_ref());
            let managed_r = environment
                .as_ref()
                .and_then(|environment| environment.r.as_ref());
            let spec = WorkerSpec {
                executable: &self.0.program,
                arguments: &self.0.arguments,
                relay: self.0.relay.as_deref(),
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
    ) -> Result<crate::resolver::ManagedPython, String> {
        self.client
            .resolve_runtime_python(self.generation.clone(), request)
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
    ) -> Result<OldGenerationCommitDisposition, String> {
        self.client
            .activate_runtime_python(self.generation.clone(), requirements, candidate)
    }
}
