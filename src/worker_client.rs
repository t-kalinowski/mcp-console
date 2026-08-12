use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

mod environment;
mod evaluation;
mod lifecycle;
mod output;

#[cfg(target_os = "macos")]
#[path = "worker_client/macos.rs"]
mod platform;

#[cfg(not(target_os = "macos"))]
#[path = "worker_client/unsupported.rs"]
mod platform;

use environment::Environment;
pub(crate) use environment::{PrepareResult, Requirements};
use evaluation::{Evaluation, EvaluationWait};
use lifecycle::{LifecycleControl, WorkerGeneration};
#[cfg(target_os = "macos")]
use output::CapturedOutputStream;
use output::{CapturedOutput, SendFailure, SendResponse};
pub(crate) use output::{Content, Response};

/// A cloneable handle to one lazily started worker.
#[derive(Clone)]
pub(crate) struct Client(Arc<ClientInner>);

struct ClientInner {
    runtime: platform::WorkerRuntime,
    program: PathBuf,
    arguments: Vec<OsString>,
    worker: Mutex<WorkerState>,
    evaluation: Mutex<Option<ActiveEvaluation>>,
    output: CapturedOutput,
    lifecycle: Mutex<LifecycleControl>,
    environment: Option<Mutex<Environment>>,
}

/// Describes one worker launch for the current runtime.
struct WorkerSpec<'a> {
    executable: &'a std::path::Path,
    arguments: &'a [OsString],
    managed_python: Option<&'a crate::resolver::ManagedPython>,
    managed_r: Option<&'a crate::resolver::ManagedR>,
}

enum WorkerState {
    Initial,
    Stopped,
    Running(platform::Worker),
}

impl WorkerState {
    fn retire(&mut self, deadline: std::time::Instant) -> Result<bool, String> {
        match self {
            Self::Running(worker) => {
                worker.shutdown(deadline)?;
                *self = Self::Stopped;
                Ok(true)
            }
            Self::Initial => Ok(false),
            Self::Stopped => Ok(false),
        }
    }

    fn finish_retirement(&mut self) -> Result<bool, String> {
        match self {
            Self::Running(worker) => {
                worker.finish_retirement()?;
                *self = Self::Stopped;
                Ok(true)
            }
            Self::Initial | Self::Stopped => Ok(false),
        }
    }
}

#[derive(Clone)]
struct ActiveEvaluation {
    generation: WorkerGeneration,
    evaluation: Arc<Evaluation>,
}

impl Client {
    pub(crate) fn new(program: PathBuf) -> Self {
        Self::with_arguments(program, Vec::new(), None)
    }

    pub(crate) fn builtin() -> Result<Self, String> {
        let program = std::env::current_exe()
            .map_err(|error| format!("failed to locate the R worker executable: {error}"))?;
        let python = crate::resolver::resolve_python(&[], |_| Ok(()))?;
        Ok(Self::with_arguments(
            program,
            vec![OsString::from("worker")],
            Some(Environment { python, r: None }),
        ))
    }

    fn with_arguments(
        program: PathBuf,
        arguments: Vec<OsString>,
        environment: Option<Environment>,
    ) -> Self {
        Self(Arc::new(ClientInner {
            runtime: platform::WorkerRuntime,
            program,
            arguments,
            worker: Mutex::new(WorkerState::Initial),
            evaluation: Mutex::new(None),
            output: CapturedOutput::new(),
            lifecycle: Mutex::new(LifecycleControl::new()),
            environment: environment.map(Mutex::new),
        }))
    }

    /// Starts one cell, supplies its stdin, or polls the cell already running.
    pub(crate) async fn send(
        &self,
        cell: Option<crate::cell::Cell>,
        stdin: Option<String>,
        timeout: Duration,
        transcript: crate::transcript::Transcript,
        call_id: u64,
    ) -> Response {
        let result = self
            .send_inner(cell, stdin, timeout, transcript, call_id)
            .await;
        self.attach_output(result)
    }

    async fn send_inner(
        &self,
        cell: Option<crate::cell::Cell>,
        stdin: Option<String>,
        timeout: Duration,
        transcript: crate::transcript::Transcript,
        call_id: u64,
    ) -> Result<SendResponse, SendFailure> {
        let generation = self.admit()?;
        let evaluation = match cell {
            Some(cell) => self.start_evaluation(cell, stdin, generation, transcript, call_id)?,
            None => match self.current_evaluation()? {
                Some(active) => {
                    self.ensure_generation(&generation)?;
                    if !active.generation.is(&generation) {
                        return Err("session restarted before the operation began"
                            .to_string()
                            .into());
                    }
                    if let Some(stdin) = stdin {
                        active.evaluation.submit_stdin(stdin)?;
                    }
                    active.evaluation
                }
                None => {
                    if let Some(stdin) = stdin {
                        self.write_idle_stdin(stdin, generation).await?;
                    }
                    return Ok(SendResponse::Idle);
                }
            },
        };

        match evaluation.wait(timeout).await? {
            EvaluationWait::Running => Ok(SendResponse::Running),
            EvaluationWait::InputRequested(output) => Ok(SendResponse::InputRequested(output)),
            EvaluationWait::Completed(result) => {
                self.clear_evaluation(&evaluation)?;
                result.map(SendResponse::Completed)
            }
        }
    }

    fn start_evaluation(
        &self,
        cell: crate::cell::Cell,
        stdin: Option<String>,
        generation: WorkerGeneration,
        transcript: crate::transcript::Transcript,
        call_id: u64,
    ) -> Result<Arc<Evaluation>, String> {
        self.ensure_generation(&generation)?;

        let evaluation = Arc::new(Evaluation::new(transcript, call_id));
        evaluation.claim_wait()?;
        if let Some(stdin) = stdin {
            evaluation.submit_stdin(stdin)?;
        }

        let mut active = self.try_evaluation()?;
        if active.is_some() {
            return Err(
                "worker is already evaluating a cell; poll without a code field".to_string(),
            );
        }
        self.ensure_generation(&generation)?;
        *active = Some(ActiveEvaluation {
            generation: generation.clone(),
            evaluation: evaluation.clone(),
        });
        drop(active);

        let client = self.clone();
        let evaluator = evaluation.clone();
        let evaluation_task = tokio::task::spawn_blocking(move || {
            client.evaluate_blocking(cell, &evaluator, generation);
        });
        let failed = evaluation.clone();
        let _completion_task = tokio::spawn(async move {
            if let Err(error) = evaluation_task.await {
                failed.complete(Err(SendFailure::from(format!(
                    "worker task failed: {error}"
                ))));
            }
        });
        Ok(evaluation)
    }

    fn current_evaluation(&self) -> Result<Option<ActiveEvaluation>, String> {
        let evaluation = self.try_evaluation()?;
        if let Some(active) = evaluation.as_ref() {
            active.evaluation.claim_wait()?;
        }
        Ok(evaluation.clone())
    }

    fn try_evaluation(&self) -> Result<MutexGuard<'_, Option<ActiveEvaluation>>, String> {
        match self.0.evaluation.try_lock() {
            Ok(evaluation) => Ok(evaluation),
            Err(std::sync::TryLockError::WouldBlock) => {
                Err("session is preparing requirements".to_string())
            }
            Err(std::sync::TryLockError::Poisoned(_)) => {
                Err("worker evaluation lock poisoned".to_string())
            }
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
        self.with_worker(
            &generation,
            |worker| worker.write_stdin(stdin),
            std::convert::identity,
        )
    }

    fn clear_evaluation(&self, completed: &Arc<Evaluation>) -> Result<(), String> {
        let mut active = self
            .0
            .evaluation
            .lock()
            .map_err(|_| "worker evaluation lock poisoned".to_string())?;
        if active
            .as_ref()
            .is_some_and(|active| Arc::ptr_eq(&active.evaluation, completed))
            && !completed.is_restarting()?
        {
            *active = None;
        }
        Ok(())
    }

    fn evaluate_blocking(
        &self,
        cell: crate::cell::Cell,
        evaluation: &Evaluation,
        generation: WorkerGeneration,
    ) {
        let resolver = self.clone();
        let checkpointer = self.clone();
        let resolver_generation = generation.clone();
        let checkpoint_generation = generation.clone();
        self.with_worker(
            &generation,
            |worker| {
                worker.evaluate(
                    cell,
                    evaluation,
                    move |request| {
                        resolver.resolve_runtime_python(resolver_generation.clone(), request)
                    },
                    move |checkpoint, candidates| {
                        checkpointer.checkpoint_runtime_python(
                            checkpoint_generation.clone(),
                            checkpoint,
                            candidates,
                        )
                    },
                )
            },
            |result| evaluation.complete(result),
        )
    }

    fn with_worker<T, U>(
        &self,
        generation: &WorkerGeneration,
        operation: impl FnOnce(&mut platform::Worker) -> Result<T, String>,
        finish: impl FnOnce(Result<T, SendFailure>) -> U,
    ) -> U {
        if let Err(error) = self.ensure_generation(generation) {
            return finish(Err(SendFailure::from(error)));
        }

        let mut worker = match self.0.worker.lock() {
            Ok(worker) => worker,
            Err(_) => {
                return finish(Err(SendFailure::from("worker lock poisoned".to_string())));
            }
        };
        if let Err(error) = self.ensure_generation(generation) {
            return finish(Err(SendFailure::from(error)));
        }

        if let Err(error) = self.start_worker(&mut worker, |stop_handle| {
            self.register_stop_handle(generation, stop_handle)
        }) {
            let error = match self.clear_worker_stop_handle(generation) {
                Ok(()) => error,
                Err(clear_error) => format!(
                    "{error}; additionally failed to clear the worker interrupt: {clear_error}"
                ),
            };
            return finish(Err(SendFailure::from(error)));
        }
        let WorkerState::Running(running) = &mut *worker else {
            unreachable!("worker should be running");
        };
        let result = match operation(running) {
            Ok(result) => Ok(result),
            Err(message) => match self.retire_failed_worker(&mut worker, generation) {
                Ok(true) => Err(SendFailure::from(message).worker_stopped()),
                Ok(false) => Err(SendFailure::from(message)),
                Err(stop_error) => Err(SendFailure::from(format!(
                    "{message}; additionally failed to stop the worker: {stop_error}"
                ))),
            },
        };
        let output = finish(result);
        drop(worker);
        output
    }

    fn start_worker(
        &self,
        worker: &mut WorkerState,
        on_started: impl FnOnce(platform::WorkerInterrupt) -> Result<(), String>,
    ) -> Result<(), String> {
        let replacing = matches!(&*worker, WorkerState::Stopped);
        if !matches!(&*worker, WorkerState::Running(_)) {
            let environment = match &self.0.environment {
                Some(environment) => Some(
                    environment
                        .lock()
                        .map_err(|_| "worker environment lock poisoned".to_string())?,
                ),
                None => None,
            };
            let managed_python = environment
                .as_ref()
                .and_then(|environment| environment.python.as_ref());
            let managed_r = environment
                .as_ref()
                .and_then(|environment| environment.r.as_ref());
            let spec = WorkerSpec {
                executable: &self.0.program,
                arguments: &self.0.arguments,
                managed_python,
                managed_r,
            };
            if replacing {
                self.0.output.push_notice(output::WORKER_STARTED_NOTICE);
            }
            let running = self
                .0
                .runtime
                .spawn(spec, self.0.output.clone(), on_started)?;
            *worker = WorkerState::Running(running);
        }
        Ok(())
    }
}
