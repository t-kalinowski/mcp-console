use std::sync::{Arc, Mutex, mpsc};
use std::thread;

use crate::worker_protocol::{ServerMessage, WorkerMessage};

use super::output::OutputCheckpoint;
use super::{Evaluation, OutputTape, WorkerCallbacks};

#[derive(Clone)]
pub(super) struct Activity(Arc<Mutex<ActivityState>>);

struct ActivityState {
    operation: Option<Operation>,
    failure: Option<String>,
    idle_input: Option<String>,
}

struct Operation {
    kind: OperationKind,
    result: mpsc::Sender<Result<OperationResult, String>>,
}

enum OperationKind {
    Cell(Arc<Evaluation>),
    PrepareR,
    PreparePython,
}

enum Route {
    Cell(Arc<Evaluation>),
    Preparation,
    Idle,
}

pub(super) enum OperationResult {
    Completed(OutputCheckpoint),
    RPrepared(String),
    RPreparationFailed(String),
    PythonPrepared(Option<crate::resolver::ManagedPython>),
    PythonPreparationFailed(String),
}

impl Activity {
    pub(super) fn new() -> Self {
        Self(Arc::new(Mutex::new(ActivityState {
            operation: None,
            failure: None,
            idle_input: None,
        })))
    }

    pub(super) fn start(
        &self,
        mut reader: crate::sideband::Reader,
        writer: crate::sideband::Writer,
        output: OutputTape,
        callbacks: WorkerCallbacks,
    ) -> thread::JoinHandle<()> {
        let activity = self.clone();
        thread::spawn(move || {
            let mut python_candidates = Vec::new();
            loop {
                let message = match reader.receive() {
                    Ok(message) => message,
                    Err(error) => {
                        activity.fail(format!("worker sideband read failed: {error}"));
                        return;
                    }
                };
                if let Err(error) = handle_message(
                    message,
                    &activity,
                    &writer,
                    &output,
                    &callbacks,
                    &mut python_candidates,
                ) {
                    activity.fail(error);
                    return;
                }
            }
        })
    }

    pub(super) fn begin_cell(
        &self,
        evaluation: Arc<Evaluation>,
    ) -> Result<mpsc::Receiver<Result<OperationResult, String>>, String> {
        let (result, receiver) = mpsc::channel();
        let mut state = self.lock()?;
        state.ensure_available()?;
        if state.idle_input.take().is_some() {
            evaluation.resume_input_request()?;
        }
        state.operation = Some(Operation {
            kind: OperationKind::Cell(evaluation),
            result,
        });
        Ok(receiver)
    }

    pub(super) fn begin_r_preparation(
        &self,
    ) -> Result<mpsc::Receiver<Result<OperationResult, String>>, String> {
        self.begin_preparation(OperationKind::PrepareR)
    }

    pub(super) fn begin_python_preparation(
        &self,
    ) -> Result<mpsc::Receiver<Result<OperationResult, String>>, String> {
        self.begin_preparation(OperationKind::PreparePython)
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
        state.operation = Some(Operation { kind, result });
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
        if let Some(operation) = operation {
            let _ = operation.result.send(Err(error));
        }
    }

    pub(super) fn snapshot(&self, output: &OutputTape) -> Result<super::WorkerSnapshot, String> {
        let state = self.lock()?;
        Ok(super::WorkerSnapshot {
            checkpoint: output.checkpoint(),
            failure: state.failure.clone(),
            input_requested: state.idle_input.is_some(),
        })
    }

    fn route(&self) -> Result<Route, String> {
        let state = self.lock()?;
        Ok(
            match state.operation.as_ref().map(|operation| &operation.kind) {
                Some(OperationKind::Cell(evaluation)) => Route::Cell(evaluation.clone()),
                Some(OperationKind::PrepareR | OperationKind::PreparePython) => Route::Preparation,
                None => Route::Idle,
            },
        )
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
            Some(OperationKind::PrepareR | OperationKind::PreparePython) => {
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
            Some(OperationKind::PrepareR | OperationKind::PreparePython) => {
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
        message: WorkerMessage,
        python_candidates: &mut Vec<crate::resolver::ManagedPython>,
        output: &OutputTape,
    ) -> Result<(), String> {
        let (Operation { kind, result }, checkpoint) = {
            let mut state = self.lock()?;
            let operation = state.operation.take().ok_or_else(|| {
                "worker sent an operation result without an active operation".to_string()
            })?;
            (operation, output.checkpoint())
        };
        let outcome = match (kind, message) {
            (OperationKind::Cell(evaluation), WorkerMessage::Completed) => {
                match evaluation.input_complete() {
                    Ok(()) => {
                        python_candidates.clear();
                        Ok(OperationResult::Completed(checkpoint))
                    }
                    Err(error) => Err(error),
                }
            }
            (OperationKind::PrepareR, WorkerMessage::RPrepared { library }) => {
                python_candidates.clear();
                Ok(OperationResult::RPrepared(library))
            }
            (OperationKind::PrepareR, WorkerMessage::RPreparationFailed { message }) => {
                python_candidates.clear();
                Ok(OperationResult::RPreparationFailed(message))
            }
            (OperationKind::PreparePython, WorkerMessage::PythonPrepared) => {
                let candidate = python_candidates.pop();
                python_candidates.clear();
                Ok(OperationResult::PythonPrepared(candidate))
            }
            (OperationKind::PreparePython, WorkerMessage::PythonPreparationFailed { message }) => {
                python_candidates.clear();
                Ok(OperationResult::PythonPreparationFailed(message))
            }
            (OperationKind::Cell(_), _) => {
                Err("worker sent an unexpected evaluation result".to_string())
            }
            (OperationKind::PrepareR, _) => {
                Err("worker sent an unexpected R preparation message".to_string())
            }
            (OperationKind::PreparePython, _) => {
                Err("worker sent an unexpected Python preparation message".to_string())
            }
        };
        match outcome {
            Ok(outcome) => result
                .send(Ok(outcome))
                .map_err(|_| "worker operation receiver stopped".to_string()),
            Err(error) => {
                let _ = result.send(Err(error.clone()));
                Err(error)
            }
        }
    }

    fn lock(&self) -> Result<std::sync::MutexGuard<'_, ActivityState>, String> {
        self.0
            .lock()
            .map_err(|_| "worker activity lock poisoned".to_string())
    }
}

impl ActivityState {
    fn ensure_available(&self) -> Result<(), String> {
        if let Some(error) = self.failure.as_ref() {
            return Err(error.clone());
        }
        if self.operation.is_some() {
            return Err("worker already has an active sideband operation".to_string());
        }
        Ok(())
    }
}

fn handle_message(
    message: WorkerMessage,
    activity: &Activity,
    writer: &crate::sideband::Writer,
    output: &OutputTape,
    callbacks: &WorkerCallbacks,
    python_candidates: &mut Vec<crate::resolver::ManagedPython>,
) -> Result<(), String> {
    use crate::worker_protocol::ConsoleChannel::{Diagnostic, Output};

    match message {
        WorkerMessage::ConsoleOutput { data } => match activity.route()? {
            Route::Cell(evaluation) => evaluation.output(Output, data),
            Route::Preparation | Route::Idle => {
                output.push_console_text(Output, data);
                Ok(())
            }
        },
        WorkerMessage::ConsoleDiagnostic { data } => match activity.route()? {
            Route::Cell(evaluation) => evaluation.output(Diagnostic, data),
            Route::Preparation | Route::Idle => {
                output.push_console_text(Diagnostic, data);
                Ok(())
            }
        },
        WorkerMessage::Image { data, mime_type } => match activity.route()? {
            Route::Cell(evaluation) => evaluation.image(data, mime_type),
            Route::Preparation | Route::Idle => {
                crate::transcript::validate_image_data(&data)?;
                output.push_image(data, mime_type, None);
                Ok(())
            }
        },
        WorkerMessage::InputRequested { prompt } => {
            let rendered = serde_json::to_string(&prompt)
                .map_err(|error| format!("failed to render worker input prompt: {error}"))?;
            activity.input_requested(prompt, rendered, output)
        }
        WorkerMessage::InputReceived => activity.input_received(),
        WorkerMessage::ResolvePython { request } => {
            let response = match callbacks.resolve_python(request) {
                Ok(managed) => {
                    let python = managed.python().to_string_lossy().into_owned();
                    python_candidates.push(managed);
                    ServerMessage::PythonResolved { python }
                }
                Err(message) => ServerMessage::PythonResolutionFailed { message },
            };
            writer
                .send(&response)
                .map_err(|error| format!("worker sideband write failed: {error}"))
        }
        WorkerMessage::ResolvePythonVersion { request } => {
            let response = match callbacks.resolve_python_version(request) {
                Ok(version) => ServerMessage::PythonVersionResolved { version },
                Err(message) => ServerMessage::PythonVersionResolutionFailed { message },
            };
            writer
                .send(&response)
                .map_err(|error| format!("worker sideband write failed: {error}"))
        }
        WorkerMessage::PythonActivated { requirements } => {
            callbacks.activate_python(requirements, python_candidates)
        }
        message @ (WorkerMessage::Completed
        | WorkerMessage::RPrepared { .. }
        | WorkerMessage::RPreparationFailed { .. }
        | WorkerMessage::PythonPrepared
        | WorkerMessage::PythonPreparationFailed { .. }) => {
            activity.complete(message, python_candidates, output)
        }
        WorkerMessage::Ready => Err("worker sent an unexpected ready message".to_string()),
    }
}
