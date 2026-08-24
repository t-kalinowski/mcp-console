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
use lifecycle::{LifecycleControl, OldGenerationCommitDisposition, WorkerGeneration};
pub(crate) use output::{Content, Response, ResponseDelivery};
use output::{OutputTape, SendFailure, SendResponse};

#[cfg(target_os = "macos")]
const DEFAULT_R_REQUIREMENTS: &[&str] = &[
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
    preparation: tokio::sync::RwLock<()>,
    output: OutputTape,
    lifecycle: Mutex<LifecycleControl>,
    environment: Option<Mutex<Environment>>,
}

/// Describes one worker launch for the current runtime.
struct WorkerSpec<'a> {
    executable: &'a std::path::Path,
    arguments: &'a [OsString],
    relay: Option<&'a std::path::Path>,
    python: Option<&'a PythonEnvironment>,
    managed_r: Option<&'a crate::resolver::ManagedR>,
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
        ))
    }

    pub(crate) fn builtin() -> Result<Self, String> {
        let python_resolver = crate::resolver::ManagedPythonResolverConfiguration::capture();
        let configured_python = std::env::var_os("RETICULATE_PYTHON");
        let program = std::env::current_exe()
            .map_err(|error| format!("failed to locate the R worker executable: {error}"))?;
        #[cfg(target_os = "macos")]
        let (r, duckdb_extensions) = {
            let r = crate::resolver::resolve_r(
                DEFAULT_R_REQUIREMENTS
                    .iter()
                    .map(|requirement| (*requirement).to_string())
                    .collect(),
                |_| Ok(()),
            )?;
            let duckdb_extensions = DEFAULT_DUCKDB_EXTENSIONS
                .iter()
                .map(|extension| (*extension).to_string())
                .collect::<Vec<_>>();
            crate::resolver::resolve_duckdb_extensions(&r, &duckdb_extensions, |_| Ok(()))?;
            (Some(r), duckdb_extensions.into_iter().collect())
        };
        #[cfg(not(target_os = "macos"))]
        let (r, duckdb_extensions) = (
            Option::<crate::resolver::ManagedR>::None,
            Default::default(),
        );
        let python = PythonEnvironment::builtin(configured_python, python_resolver, r.as_ref())?;
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
        ))
    }

    fn with_arguments(
        program: PathBuf,
        arguments: Vec<OsString>,
        relay: Option<PathBuf>,
        environment: Option<Environment>,
    ) -> Self {
        Self(Arc::new(ClientInner {
            runtime: platform::WorkerRuntime,
            program,
            arguments,
            relay,
            worker: Mutex::new(WorkerState::Initial),
            evaluation: Mutex::new(None),
            preparation: tokio::sync::RwLock::new(()),
            output: OutputTape::new(),
            lifecycle: Mutex::new(LifecycleControl::new()),
            environment: environment.map(Mutex::new),
        }))
    }

    /// Starts one cell, supplies stdin, or collects an idle response.
    pub(crate) async fn send(
        &self,
        cell: Option<crate::cell::Cell>,
        stdin: Option<String>,
        requirements: Option<Requirements>,
        timeout: Duration,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Response {
        if let Some(requirements) = requirements {
            let Some(cell) = cell else {
                return output::direct_failure("`requirements` requires a code cell");
            };
            return self
                .send_with_requirements(cell, stdin, requirements, timeout, transcript, call_id)
                .await;
        }
        match self
            .send_inner(cell, stdin, timeout, transcript, call_id)
            .await
        {
            Ok(response) => output::render_response(response),
            Err(failure) => output::direct_failure(failure.message),
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
        let generation = self.admit()?;
        let preparation = self.admit_send()?;
        let (evaluation, wait_claim) = match cell {
            Some(cell) => self.start_evaluation(cell, stdin, generation, transcript, call_id)?,
            None => match self.current_evaluation()? {
                Some(active) => {
                    self.ensure_generation(&generation)?;
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
        self.ensure_generation(&generation)?;

        let mut active = self.evaluation()?;
        if let Some(active) = active.as_ref() {
            return Err(active.evaluation.reject_new_cell_message().to_string());
        }
        self.ensure_generation(&generation)?;
        let prelude = self.0.output.take_prelude();
        let evaluation = Arc::new(Evaluation::new(
            transcript,
            call_id,
            self.0.output.clone(),
            prelude,
        ));
        let wait_claim = evaluation.claim()?;
        if let Some(stdin) = stdin {
            evaluation.submit_stdin(stdin)?;
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
