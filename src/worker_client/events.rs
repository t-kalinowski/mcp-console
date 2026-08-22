use std::sync::{Arc, Mutex, mpsc};
use std::thread;

use crate::relay_protocol::{RelayCommand, RelayEvent};

use super::{
    Evaluation, OutputTape, PythonPreparationCommit, RPreparationCommit, WorkerCallbacks,
    WorkerProcessOutcome,
};

#[derive(Clone)]
pub(super) struct WorkerOperationState(Arc<Mutex<OperationState>>);

pub(super) enum WorkerEvent {
    Relay(RelayEvent),
    TransportFailure(String),
    RetireOperation {
        error: String,
        reached: mpsc::SyncSender<()>,
    },
    RelayClosed,
}

pub(super) enum ReadyCommitOutcome {
    Committed,
    Failed(String),
    Retiring,
}

pub(super) struct WorkerEventDispatcher {
    thread: thread::JoinHandle<Option<WorkerProcessOutcome>>,
}

struct OperationState {
    operation: Option<Operation>,
    failure: Option<String>,
    idle_input: Option<String>,
    retiring: bool,
}

struct Operation {
    kind: OperationKind,
    result: Option<mpsc::Sender<Result<OperationResult, String>>>,
}

enum OperationKind {
    Cell(Arc<Evaluation>),
    PrepareR {
        library: String,
        commit: RPreparationCommit,
    },
    PreparePython {
        commit: PythonPreparationCommit,
    },
}

enum Route {
    Cell(Arc<Evaluation>),
    Preparation,
    Idle,
}

pub(super) enum OperationResult {
    Completed,
    RPrepared(super::PreparationOutcome),
    PythonPrepared(super::PreparationOutcome),
}

impl WorkerOperationState {
    pub(super) fn new() -> Self {
        Self(Arc::new(Mutex::new(OperationState {
            operation: None,
            failure: None,
            idle_input: None,
            retiring: false,
        })))
    }

    pub(super) fn begin_cell(
        &self,
        evaluation: Arc<Evaluation>,
        capture_idle_prelude: bool,
    ) -> Result<mpsc::Receiver<Result<OperationResult, String>>, String> {
        let (result, receiver) = mpsc::channel();
        let mut state = self.lock()?;
        state.ensure_available()?;
        if state.idle_input.take().is_some() {
            evaluation.resume_input_request()?;
        }
        let mut operation = Some(Operation {
            kind: OperationKind::Cell(evaluation.clone()),
            result: Some(result),
        });
        if capture_idle_prelude {
            evaluation.capture_prelude_before(|| state.operation = operation.take())?;
        } else {
            state.operation = operation.take();
        }
        Ok(receiver)
    }

    pub(super) fn begin_r_preparation(
        &self,
        library: String,
        commit: RPreparationCommit,
    ) -> Result<mpsc::Receiver<Result<OperationResult, String>>, String> {
        self.begin_preparation(OperationKind::PrepareR { library, commit })
    }

    pub(super) fn begin_python_preparation(
        &self,
        commit: PythonPreparationCommit,
    ) -> Result<mpsc::Receiver<Result<OperationResult, String>>, String> {
        self.begin_preparation(OperationKind::PreparePython { commit })
    }

    fn begin_preparation(
        &self,
        kind: OperationKind,
    ) -> Result<mpsc::Receiver<Result<OperationResult, String>>, String> {
        let (result, receiver) = mpsc::channel();
        let mut state = self.lock()?;
        state.ensure_available()?;
        if let Some(prompt) = state.idle_input.as_ref() {
            return Err(format!(
                "idle R callback requested input {prompt} during requirement preparation; collect callback input with send before preparing requirements"
            ));
        }
        state.operation = Some(Operation {
            kind,
            result: Some(result),
        });
        Ok(receiver)
    }

    pub(super) fn fail(&self, error: String) {
        let operation = {
            let Ok(mut state) = self.0.lock() else {
                return;
            };
            if state.failure.is_none() {
                state.failure = Some(error.clone());
            }
            state.operation.take()
        };
        if let Some(result) = operation.and_then(|operation| operation.result) {
            let _ = result.send(Err(error));
        }
    }

    pub(super) fn retire_operation(&self, error: String) {
        let result = {
            let Ok(mut state) = self.0.lock() else {
                return;
            };
            state.retiring = true;
            state
                .operation
                .as_mut()
                .and_then(|operation| operation.result.take())
        };
        if let Some(result) = result {
            let _ = result.send(Err(error));
        }
    }

    pub(super) fn has_failure(&self) -> Result<bool, String> {
        Ok(self.lock()?.failure.is_some())
    }

    pub(super) fn idle_response_snapshot(
        &self,
        output: &OutputTape,
    ) -> Result<super::IdleResponseSnapshot, String> {
        let state = self.lock()?;
        Ok(super::IdleResponseSnapshot {
            cut: output.cut(),
            failure: state.failure.clone(),
            input_requested: state.idle_input.is_some(),
        })
    }

    fn with_route<T>(&self, publish: impl FnOnce(Route) -> Result<T, String>) -> Result<T, String> {
        let state = self.lock()?;
        let route = match state.operation.as_ref().map(|operation| &operation.kind) {
            Some(OperationKind::Cell(evaluation)) => Route::Cell(evaluation.clone()),
            Some(OperationKind::PrepareR { .. } | OperationKind::PreparePython { .. }) => {
                Route::Preparation
            }
            None => Route::Idle,
        };
        publish(route)
    }

    fn input_requested(
        &self,
        prompt: String,
        rendered: String,
        output: &OutputTape,
    ) -> Result<(), String> {
        let mut state = self.lock()?;
        match state.operation.as_ref().map(|operation| &operation.kind) {
            Some(OperationKind::Cell(evaluation)) => evaluation.input_requested(prompt),
            Some(OperationKind::PrepareR { .. } | OperationKind::PreparePython { .. }) => {
                output.push_notice_line(format!("input requested: {rendered}"));
                Err(format!(
                    "idle R callback requested input {rendered} during requirement preparation; collect callback input with send before preparing requirements"
                ))
            }
            None => {
                if state.idle_input.is_some() {
                    return Err(
                        "worker requested new input before receiving prior input".to_string()
                    );
                }
                state.idle_input = Some(rendered.clone());
                output.push_notice_line(format!("input requested: {rendered}"));
                Ok(())
            }
        }
    }

    fn input_received(&self) -> Result<(), String> {
        let mut state = self.lock()?;
        match state.operation.as_ref().map(|operation| &operation.kind) {
            Some(OperationKind::Cell(evaluation)) => evaluation.input_received(),
            Some(OperationKind::PrepareR { .. } | OperationKind::PreparePython { .. }) => {
                Err("worker reported received input during requirement preparation".to_string())
            }
            None => {
                state.idle_input.take().ok_or_else(|| {
                    "worker reported received input without requesting it".to_string()
                })?;
                Ok(())
            }
        }
    }

    fn complete(
        &self,
        event: RelayEvent,
        python_candidates: &mut Vec<crate::resolver::ManagedPython>,
    ) -> Result<(), String> {
        let Operation { kind, result } = {
            let mut state = self.lock()?;
            state.operation.take().ok_or_else(|| {
                "worker sent an operation result without an active operation".to_string()
            })?
        };

        if result.is_none() && kind.matches_result(&event) {
            python_candidates.clear();
            return Ok(());
        }

        let committed = match (kind, event) {
            (OperationKind::Cell(evaluation), RelayEvent::Completed) => {
                match evaluation.input_complete() {
                    Ok(()) => {
                        python_candidates.clear();
                        evaluation.complete_cell_after_grace();
                        Ok(OperationResult::Completed)
                    }
                    Err(error) => Err(error),
                }
            }
            (
                OperationKind::PrepareR {
                    library: expected,
                    commit,
                },
                RelayEvent::RPrepared { library },
            ) if library == expected => {
                python_candidates.clear();
                commit(Ok(())).map(OperationResult::RPrepared)
            }
            (
                OperationKind::PrepareR { commit, .. },
                RelayEvent::RPreparationFailed { message },
            ) => {
                python_candidates.clear();
                commit(Err(message)).map(OperationResult::RPrepared)
            }
            (OperationKind::PreparePython { commit }, RelayEvent::PythonPrepared) => {
                let candidate = python_candidates.pop();
                python_candidates.clear();
                commit(Ok(candidate)).map(OperationResult::PythonPrepared)
            }
            (
                OperationKind::PreparePython { commit },
                RelayEvent::PythonPreparationFailed { message },
            ) => {
                python_candidates.clear();
                commit(Err(message)).map(OperationResult::PythonPrepared)
            }
            (OperationKind::Cell(_), _) => {
                Err("worker sent an unexpected evaluation result".to_string())
            }
            (OperationKind::PrepareR { .. }, RelayEvent::RPrepared { .. }) => {
                Err("worker prepared an unexpected R library".to_string())
            }
            (OperationKind::PrepareR { .. }, _) => {
                Err("worker sent an unexpected R preparation message".to_string())
            }
            (OperationKind::PreparePython { .. }, _) => {
                Err("worker sent an unexpected Python preparation message".to_string())
            }
        };

        match (result, committed) {
            (Some(result), Ok(committed)) => result
                .send(Ok(committed))
                .map_err(|_| "worker operation receiver stopped".to_string()),
            (Some(result), Err(error)) => {
                let _ = result.send(Err(error.clone()));
                Err(error)
            }
            (None, Err(error)) => Err(error),
            (None, Ok(_)) => unreachable!("a matching cancelled operation result returned early"),
        }
    }

    fn lock(&self) -> Result<std::sync::MutexGuard<'_, OperationState>, String> {
        self.0
            .lock()
            .map_err(|_| "worker operation state lock poisoned".to_string())
    }
}

impl OperationKind {
    fn matches_result(&self, event: &RelayEvent) -> bool {
        match (self, event) {
            (Self::Cell(_), RelayEvent::Completed)
            | (Self::PrepareR { .. }, RelayEvent::RPreparationFailed { .. })
            | (
                Self::PreparePython { .. },
                RelayEvent::PythonPrepared | RelayEvent::PythonPreparationFailed { .. },
            ) => true,
            (
                Self::PrepareR {
                    library: expected, ..
                },
                RelayEvent::RPrepared { library },
            ) => library == expected,
            _ => false,
        }
    }
}

impl OperationState {
    fn ensure_available(&self) -> Result<(), String> {
        if let Some(error) = self.failure.as_ref() {
            return Err(error.clone());
        }
        if self.retiring {
            return Err("worker is retiring".to_string());
        }
        if self.operation.is_some() {
            return Err("worker already has an active operation".to_string());
        }
        Ok(())
    }
}

impl WorkerEventDispatcher {
    #[allow(clippy::too_many_arguments)]
    pub(super) fn start(
        events: mpsc::Receiver<WorkerEvent>,
        operation: WorkerOperationState,
        commands: super::platform::RelayCommandSender,
        output: OutputTape,
        callbacks: WorkerCallbacks,
        startup: mpsc::SyncSender<Result<(), String>>,
        ready_commit: mpsc::Receiver<ReadyCommitOutcome>,
        interrupts: super::platform::InterruptRequests,
        shutdown_started: super::platform::ShutdownAcceptance,
    ) -> Self {
        let thread = thread::spawn(move || {
            dispatch_worker_events(
                events,
                operation,
                commands,
                output,
                callbacks,
                startup,
                ready_commit,
                interrupts,
                shutdown_started,
            )
        });
        Self { thread }
    }

    pub(super) fn join(self) -> Result<Option<WorkerProcessOutcome>, String> {
        self.thread
            .join()
            .map_err(|_| "worker event dispatcher task failed".to_string())
    }
}

#[allow(clippy::too_many_arguments)]
fn dispatch_worker_events(
    events: mpsc::Receiver<WorkerEvent>,
    operation: WorkerOperationState,
    commands: super::platform::RelayCommandSender,
    output: OutputTape,
    callbacks: WorkerCallbacks,
    startup: mpsc::SyncSender<Result<(), String>>,
    ready_commit: mpsc::Receiver<ReadyCommitOutcome>,
    interrupts: super::platform::InterruptRequests,
    shutdown_started: super::platform::ShutdownAcceptance,
) -> Option<WorkerProcessOutcome> {
    let mut startup = Some(startup);
    let stdout = output.direct_stdout();
    let stderr = output.direct_stderr();
    let mut python_candidates = Vec::new();
    let mut runtime_started = false;
    let mut semantic_failure = false;
    let mut stdout_closed = false;
    let mut stderr_closed = false;
    let mut sideband_closed = false;
    let mut relay_fatal = false;
    let mut intentional_shutdown = false;
    let mut retiring = false;
    let mut process_outcome = None;
    let mut relay_closed = false;

    while let Ok(event) = events.recv() {
        match event {
            WorkerEvent::Relay(event) => {
                if process_outcome.is_some() {
                    fail_dispatch(
                        &operation,
                        &mut startup,
                        &interrupts,
                        "worker relay sent an event after worker outcome".to_string(),
                    );
                    semantic_failure = true;
                    continue;
                }
                let result = match event {
                    RelayEvent::Stdout { data } => {
                        if stdout_closed {
                            Err("worker relay sent stdout after closing the stream".to_string())
                        } else {
                            stdout.push(data.as_bytes());
                            Ok(())
                        }
                    }
                    RelayEvent::StdoutBytes { data } => {
                        if stdout_closed {
                            Err("worker relay sent stdout after closing the stream".to_string())
                        } else {
                            data.decode().map(|data| stdout.push(&data))
                        }
                    }
                    RelayEvent::Stderr { data } => {
                        if stderr_closed {
                            Err("worker relay sent stderr after closing the stream".to_string())
                        } else {
                            stderr.push(data.as_bytes());
                            Ok(())
                        }
                    }
                    RelayEvent::StderrBytes { data } => {
                        if stderr_closed {
                            Err("worker relay sent stderr after closing the stream".to_string())
                        } else {
                            data.decode().map(|data| stderr.push(&data))
                        }
                    }
                    RelayEvent::StdoutClosed => {
                        if stdout_closed {
                            Err("worker relay closed stdout twice".to_string())
                        } else {
                            stdout.close();
                            stdout_closed = true;
                            Ok(())
                        }
                    }
                    RelayEvent::StderrClosed => {
                        if stderr_closed {
                            Err("worker relay closed stderr twice".to_string())
                        } else {
                            stderr.close();
                            stderr_closed = true;
                            Ok(())
                        }
                    }
                    RelayEvent::WorkerSidebandClosed => {
                        if sideband_closed {
                            Err("worker relay closed the worker sideband twice".to_string())
                        } else {
                            sideband_closed = true;
                            if !intentional_shutdown && !semantic_failure && !retiring {
                                fail_dispatch(
                                    &operation,
                                    &mut startup,
                                    &interrupts,
                                    "worker sideband read failed: worker sideband closed"
                                        .to_string(),
                                );
                                semantic_failure = true;
                            }
                            Ok(())
                        }
                    }
                    RelayEvent::InterruptResult { request_id, error } => {
                        if semantic_failure || retiring {
                            Ok(())
                        } else {
                            interrupts.complete(request_id, error)
                        }
                    }
                    RelayEvent::ShutdownStarted => {
                        intentional_shutdown = true;
                        shutdown_started.observe()
                    }
                    RelayEvent::WorkerExited { code } => {
                        process_outcome = Some(WorkerProcessOutcome::Exited(code));
                        Ok(())
                    }
                    RelayEvent::WorkerSignaled { signal } => {
                        process_outcome = Some(WorkerProcessOutcome::Signaled(signal));
                        Ok(())
                    }
                    RelayEvent::Fatal { message } => {
                        if relay_fatal && !retiring {
                            Err("worker relay reported two fatal failures".to_string())
                        } else {
                            relay_fatal = true;
                            if !retiring {
                                fail_dispatch(&operation, &mut startup, &interrupts, message);
                                semantic_failure = true;
                            }
                            Ok(())
                        }
                    }
                    _ if sideband_closed => Err(
                        "worker relay sent a semantic event after closing the worker sideband"
                            .to_string(),
                    ),
                    _ if semantic_failure => Ok(()),
                    semantic if retiring && ignored_during_retirement(&semantic) => Ok(()),
                    RelayEvent::Ready => {
                        if runtime_started || startup.is_none() {
                            Err("worker sent an unexpected ready message".to_string())
                        } else {
                            if let Some(startup) = startup.take() {
                                let _ = startup.send(Ok(()));
                            }
                            // Commit readiness to generation-owned lifecycle state before
                            // dispatching callbacks already queued behind Ready. Otherwise a
                            // callback could mutate state for a worker not yet admitted.
                            match ready_commit.recv() {
                                Ok(ReadyCommitOutcome::Committed) => {
                                    runtime_started = true;
                                    Ok(())
                                }
                                Ok(ReadyCommitOutcome::Failed(error)) => Err(error),
                                Ok(ReadyCommitOutcome::Retiring) => {
                                    operation.retire_operation(
                                        "worker stopped before operation completed".to_string(),
                                    );
                                    retiring = true;
                                    runtime_started = true;
                                    Ok(())
                                }
                                Err(_) => Err("worker readiness commit stopped".to_string()),
                            }
                        }
                    }
                    semantic if !runtime_started => Err(startup_semantic_error(&semantic)),
                    semantic => handle_semantic_event(
                        semantic,
                        &operation,
                        &commands,
                        &output,
                        &callbacks,
                        &mut python_candidates,
                    ),
                };
                if let Err(error) = result {
                    fail_dispatch(&operation, &mut startup, &interrupts, error);
                    semantic_failure = true;
                }
            }
            WorkerEvent::TransportFailure(error) => {
                if !retiring {
                    fail_dispatch(&operation, &mut startup, &interrupts, error);
                    semantic_failure = true;
                }
            }
            WorkerEvent::RetireOperation { error, reached } => {
                if let Some(startup) = startup.take() {
                    let _ = startup.send(Err(error.clone()));
                }
                interrupts.fail(error.clone());
                operation.retire_operation(error);
                retiring = true;
                let _ = reached.send(());
                if relay_closed {
                    break;
                }
            }
            WorkerEvent::RelayClosed => {
                relay_closed = true;
                if !retiring
                    && !(stdout_closed
                        && stderr_closed
                        && sideband_closed
                        && process_outcome.is_some())
                {
                    fail_dispatch(
                        &operation,
                        &mut startup,
                        &interrupts,
                        "worker relay stdout closed before retirement completed".to_string(),
                    );
                    semantic_failure = true;
                }
                if retiring || semantic_failure || !intentional_shutdown {
                    break;
                }
            }
        }
    }

    if !stdout_closed {
        stdout.close();
    }
    if !stderr_closed {
        stderr.close();
    }
    if !relay_closed && !retiring {
        fail_dispatch(
            &operation,
            &mut startup,
            &interrupts,
            "worker event queue closed".to_string(),
        );
    }
    interrupts.fail("worker stopped before interrupt completed".to_string());
    process_outcome
}

fn ignored_during_retirement(event: &RelayEvent) -> bool {
    matches!(
        event,
        RelayEvent::Ready
            | RelayEvent::InputRequested { .. }
            | RelayEvent::InputReceived
            | RelayEvent::InputCancelled
            | RelayEvent::ResolvePython { .. }
            | RelayEvent::ResolvePythonVersion { .. }
            | RelayEvent::PythonActivated { .. }
    )
}

fn fail_dispatch(
    operation: &WorkerOperationState,
    startup: &mut Option<mpsc::SyncSender<Result<(), String>>>,
    interrupts: &super::platform::InterruptRequests,
    error: String,
) {
    if let Some(startup) = startup.take() {
        let _ = startup.send(Err(error.clone()));
    }
    interrupts.fail(error.clone());
    operation.fail(error);
}

fn startup_semantic_error(event: &RelayEvent) -> String {
    match event {
        RelayEvent::ConsoleOutput { data } | RelayEvent::ConsoleDiagnostic { data } => {
            format!("worker emitted output before readiness: {data}")
        }
        RelayEvent::Image { .. } => "worker emitted an image before readiness".to_string(),
        _ => "worker did not report readiness".to_string(),
    }
}

fn handle_semantic_event(
    event: RelayEvent,
    operation: &WorkerOperationState,
    commands: &super::platform::RelayCommandSender,
    output: &OutputTape,
    callbacks: &WorkerCallbacks,
    python_candidates: &mut Vec<crate::resolver::ManagedPython>,
) -> Result<(), String> {
    use crate::worker_protocol::ConsoleChannel::{Diagnostic, Output};

    match event {
        RelayEvent::ConsoleOutput { data } => operation.with_route(|route| match route {
            Route::Cell(evaluation) => evaluation.output(Output, data),
            Route::Preparation | Route::Idle => {
                output.push_console_text(Output, data);
                Ok(())
            }
        }),
        RelayEvent::ConsoleDiagnostic { data } => operation.with_route(|route| match route {
            Route::Cell(evaluation) => evaluation.output(Diagnostic, data),
            Route::Preparation | Route::Idle => {
                output.push_console_text(Diagnostic, data);
                Ok(())
            }
        }),
        RelayEvent::Image { data, mime_type } => operation.with_route(|route| match route {
            Route::Cell(evaluation) => evaluation.image(data, mime_type),
            Route::Preparation | Route::Idle => {
                crate::transcript::validate_image_data(&data)?;
                output.push_image(data, mime_type, None);
                Ok(())
            }
        }),
        RelayEvent::InputRequested { prompt } => {
            let rendered = serde_json::to_string(&prompt)
                .map_err(|error| format!("failed to render worker input prompt: {error}"))?;
            operation.input_requested(prompt, rendered, output)
        }
        RelayEvent::InputReceived | RelayEvent::InputCancelled => operation.input_received(),
        RelayEvent::ResolvePython { request } => {
            let response = match callbacks.resolve_python(request) {
                Ok(managed) => {
                    let python = managed.python().to_string_lossy().into_owned();
                    python_candidates.push(managed);
                    RelayCommand::PythonResolved { python }
                }
                Err(message) => RelayCommand::PythonResolutionFailed { message },
            };
            commands.send(response)
        }
        RelayEvent::ResolvePythonVersion { request } => {
            let response = match callbacks.resolve_python_version(request) {
                Ok(version) => RelayCommand::PythonVersionResolved { version },
                Err(message) => RelayCommand::PythonVersionResolutionFailed { message },
            };
            commands.send(response)
        }
        RelayEvent::PythonActivated { requirements } => callbacks
            .activate_python(requirements, python_candidates)
            .map(|_| ()),
        event @ (RelayEvent::Completed
        | RelayEvent::RPrepared { .. }
        | RelayEvent::RPreparationFailed { .. }
        | RelayEvent::PythonPrepared
        | RelayEvent::PythonPreparationFailed { .. }) => {
            operation.complete(event, python_candidates)
        }
        RelayEvent::Ready
        | RelayEvent::Stdout { .. }
        | RelayEvent::StdoutBytes { .. }
        | RelayEvent::Stderr { .. }
        | RelayEvent::StderrBytes { .. }
        | RelayEvent::StdoutClosed
        | RelayEvent::StderrClosed
        | RelayEvent::WorkerSidebandClosed
        | RelayEvent::InterruptResult { .. }
        | RelayEvent::ShutdownStarted
        | RelayEvent::WorkerExited { .. }
        | RelayEvent::WorkerSignaled { .. }
        | RelayEvent::Fatal { .. } => unreachable!("non-semantic relay event reached dispatcher"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::worker_client::EvaluationWait;
    use crate::worker_client::output::{Content, Response, SendResponse, render_response};
    use crate::worker_protocol::ConsoleChannel::Output;

    #[tokio::test]
    async fn cell_admission_captures_an_inflight_idle_route_as_prelude() {
        let output = OutputTape::new();
        let operation = WorkerOperationState::new();
        let evaluation = Arc::new(Evaluation::new(
            crate::transcript::Transcript::new(),
            None,
            output.clone(),
            Response::default(),
        ));
        let claim = evaluation.claim().unwrap();
        let (routed, routed_rx) = mpsc::sync_channel(0);
        let (release, release_rx) = mpsc::sync_channel(0);

        let idle_operation = operation.clone();
        let idle_output = output.clone();
        let idle = thread::spawn(move || {
            idle_operation.with_route(|route| {
                assert!(matches!(route, Route::Idle));
                routed.send(()).unwrap();
                release_rx.recv().unwrap();
                idle_output.push_console_text(Output, "idle output");
                Ok(())
            })
        });
        routed_rx.recv().unwrap();

        let admitting_operation = operation.clone();
        let admitting_evaluation = evaluation.clone();
        let (contending, contending_rx) = mpsc::sync_channel(0);
        let admission = thread::spawn(move || {
            assert!(matches!(
                admitting_operation.0.try_lock(),
                Err(std::sync::TryLockError::WouldBlock)
            ));
            contending.send(()).unwrap();
            admitting_operation.begin_cell(admitting_evaluation, true)
        });
        contending_rx.recv().unwrap();
        release.send(()).unwrap();
        idle.join().unwrap().unwrap();
        drop(admission.join().unwrap().unwrap());

        operation
            .with_route(|route| match route {
                Route::Cell(evaluation) => evaluation.output(Output, "cell output".to_string()),
                Route::Preparation | Route::Idle => panic!("cell route was not installed"),
            })
            .unwrap();
        evaluation.complete_cell(Ok(()));
        let EvaluationWait::Completed(response) = evaluation
            .wait(claim, std::time::Duration::ZERO)
            .await
            .unwrap()
        else {
            panic!("cell did not complete")
        };
        let response = render_response(SendResponse::Completed(response));
        let (content, is_error, delivery) = response.into_parts();
        assert!(!is_error);
        assert!(matches!(
            content.as_slice(),
            [Content::Text(text)]
                if text == "idle output\n[output produced while idle]\ncell output"
        ));
        delivery.unwrap().delivered();
    }
}
