use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const INPUT_REQUEST_GRACE: Duration = Duration::from_millis(10);
#[cfg(target_os = "macos")]
const WORKER_RESTART_BANNER: &str = "[worker restarted: in-memory state lost]\n";

/// A cloneable handle to one lazily started worker.
#[derive(Clone)]
pub(crate) struct Client(Arc<ClientInner>);

struct ClientInner {
    program: PathBuf,
    arguments: Vec<OsString>,
    worker: Mutex<WorkerState>,
    evaluation: Mutex<Option<Arc<Evaluation>>>,
    output: CapturedOutput,
    shutdown_gate: Mutex<ShutdownGate>,
    managed_python: Option<crate::python::Managed>,
}

enum WorkerState {
    Initial,
    ReplacementPending,
    Running(platform::Worker),
}

#[cfg(target_os = "macos")]
#[derive(Clone)]
struct CapturedOutput(Arc<Mutex<CapturedOutputState>>);

#[cfg(not(target_os = "macos"))]
#[derive(Clone)]
struct CapturedOutput;

#[cfg(target_os = "macos")]
#[derive(Default)]
struct CapturedOutputState {
    streams: Vec<Option<Vec<u8>>>,
    events: Vec<CapturedOutputEvent>,
    restart_notice: String,
}

#[cfg(target_os = "macos")]
enum CapturedOutputEvent {
    Data { stream: usize, bytes: Vec<u8> },
    Closed { stream: usize },
}

#[cfg(target_os = "macos")]
struct CapturedOutputStream {
    output: CapturedOutput,
    stream: usize,
}

struct Evaluation {
    state: Mutex<EvaluationState>,
    changed: tokio::sync::Notify,
}

#[derive(Default)]
pub(crate) struct Response {
    content: Vec<Content>,
    is_error: bool,
}

pub(crate) enum Content {
    Text(String),
    Image { data: String, mime_type: String },
}

struct EvaluationState {
    result: Option<Result<Response, SendFailure>>,
    output: Response,
    input_report_at: Option<Instant>,
    #[cfg(target_os = "macos")]
    stdin: Option<platform::StdinSender>,
    pending_stdin: Vec<u8>,
}

enum EvaluationWait {
    Running,
    InputRequested(Response),
    Completed(Result<Response, SendFailure>),
}

enum SendResponse {
    Idle,
    Running,
    InputRequested(Response),
    Completed(Response),
}

struct SendFailure {
    output: Response,
    message: String,
}

impl From<String> for SendFailure {
    fn from(message: String) -> Self {
        Self {
            output: Response::default(),
            message,
        }
    }
}

enum EvaluationStatus {
    Waiting,
    Grace(Duration),
    Report(EvaluationWait),
}

impl Response {
    pub(crate) fn into_parts(self) -> (Vec<Content>, bool) {
        (self.content, self.is_error)
    }

    fn push_text(&mut self, text: impl Into<String>) {
        let text = text.into();
        if text.is_empty() {
            return;
        }
        if let Some(Content::Text(output)) = self.content.last_mut() {
            output.push_str(&text);
        } else {
            self.content.push(Content::Text(text));
        }
    }

    fn push_image(&mut self, data: String, mime_type: String) {
        self.content.push(Content::Image { data, mime_type });
    }

    fn extend(&mut self, other: Self) {
        for content in other.content {
            match content {
                Content::Text(text) => self.push_text(text),
                Content::Image { data, mime_type } => self.push_image(data, mime_type),
            }
        }
    }

    fn is_empty(&self) -> bool {
        self.content.is_empty()
    }

    fn text_needs_newline(&self) -> bool {
        !matches!(self.content.last(), Some(Content::Text(text)) if text.ends_with('\n'))
    }
}

/// Keeps the current stop handle available until shutdown closes the gate.
enum ShutdownGate {
    Open {
        stop_handle: Option<platform::StopHandle>,
    },
    Closed {
        deadline: Instant,
    },
}

impl Client {
    pub(crate) fn new(program: PathBuf) -> Self {
        Self::with_arguments(program, Vec::new(), None)
    }

    pub(crate) fn builtin() -> Result<Self, String> {
        let program = std::env::current_exe()
            .map_err(|error| format!("failed to locate the R worker executable: {error}"))?;
        let managed_python = crate::python::preflight()?;
        Ok(Self::with_arguments(
            program,
            vec![OsString::from("worker")],
            managed_python,
        ))
    }

    fn with_arguments(
        program: PathBuf,
        arguments: Vec<OsString>,
        managed_python: Option<crate::python::Managed>,
    ) -> Self {
        Self(Arc::new(ClientInner {
            program,
            arguments,
            worker: Mutex::new(WorkerState::Initial),
            evaluation: Mutex::new(None),
            output: CapturedOutput::new(),
            shutdown_gate: Mutex::new(ShutdownGate::Open { stop_handle: None }),
            managed_python,
        }))
    }

    /// Starts one cell, supplies its stdin, or polls the cell already running.
    pub(crate) async fn send(
        &self,
        cell: Option<crate::cell::Cell>,
        stdin: Option<String>,
        timeout: Duration,
    ) -> Response {
        let result = self.send_inner(cell, stdin, timeout).await;
        self.attach_output(result)
    }

    async fn send_inner(
        &self,
        cell: Option<crate::cell::Cell>,
        stdin: Option<String>,
        timeout: Duration,
    ) -> Result<SendResponse, SendFailure> {
        let evaluation = match cell {
            Some(cell) => self.start_evaluation(cell, stdin)?,
            None => match self.current_evaluation()? {
                Some(evaluation) => {
                    if let Some(stdin) = stdin {
                        evaluation.submit_stdin(stdin)?;
                    }
                    evaluation
                }
                None => {
                    if let Some(stdin) = stdin {
                        self.write_idle_stdin(stdin).await?;
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
    ) -> Result<Arc<Evaluation>, String> {
        if self.shutdown_requested()? {
            return Err("worker is shutting down".to_string());
        }

        let evaluation = Arc::new(Evaluation {
            state: Mutex::new(EvaluationState {
                result: None,
                output: Response::default(),
                input_report_at: None,
                #[cfg(target_os = "macos")]
                stdin: None,
                pending_stdin: Vec::new(),
            }),
            changed: tokio::sync::Notify::new(),
        });
        if let Some(stdin) = stdin {
            evaluation.submit_stdin(stdin)?;
        }

        let mut active = self
            .0
            .evaluation
            .lock()
            .map_err(|_| "worker evaluation lock poisoned".to_string())?;
        if active.is_some() {
            return Err(
                "worker is already evaluating a cell; poll without a code field".to_string(),
            );
        }
        if self.shutdown_requested()? {
            return Err("worker is shutting down".to_string());
        }
        *active = Some(evaluation.clone());
        drop(active);

        let client = self.clone();
        let running = evaluation.clone();
        let evaluator = evaluation.clone();
        let evaluation_task =
            tokio::task::spawn_blocking(move || client.evaluate_blocking(cell, &evaluator));
        let _completion_task = tokio::spawn(async move {
            let result = evaluation_task
                .await
                .map_err(|error| format!("worker task failed: {error}"))
                .and_then(|result| result);
            running.complete(result);
        });
        Ok(evaluation)
    }

    fn current_evaluation(&self) -> Result<Option<Arc<Evaluation>>, String> {
        self.0
            .evaluation
            .lock()
            .map(|evaluation| evaluation.clone())
            .map_err(|_| "worker evaluation lock poisoned".to_string())
    }

    async fn write_idle_stdin(&self, stdin: String) -> Result<(), String> {
        if stdin.is_empty() {
            return Ok(());
        }
        let client = self.clone();
        tokio::task::spawn_blocking(move || client.write_idle_stdin_blocking(stdin))
            .await
            .map_err(|error| format!("worker stdin task failed: {error}"))?
    }

    fn write_idle_stdin_blocking(&self, stdin: String) -> Result<(), String> {
        self.with_worker(|worker| worker.write_stdin(stdin))
    }

    fn clear_evaluation(&self, completed: &Arc<Evaluation>) -> Result<(), String> {
        let mut active = self
            .0
            .evaluation
            .lock()
            .map_err(|_| "worker evaluation lock poisoned".to_string())?;
        if active
            .as_ref()
            .is_some_and(|active| Arc::ptr_eq(active, completed))
        {
            *active = None;
        }
        Ok(())
    }

    fn evaluate_blocking(
        &self,
        cell: crate::cell::Cell,
        evaluation: &Evaluation,
    ) -> Result<(), String> {
        self.with_worker(|worker| worker.evaluate(cell, evaluation))
    }

    fn with_worker<T>(
        &self,
        operation: impl FnOnce(&mut platform::Worker) -> Result<T, String>,
    ) -> Result<T, String> {
        if self.shutdown_requested()? {
            return Err("worker is shutting down".to_string());
        }

        let mut worker = self
            .0
            .worker
            .lock()
            .map_err(|_| "worker lock poisoned".to_string())?;
        if self.shutdown_requested()? {
            return Err("worker is shutting down".to_string());
        }

        let replacing = matches!(&*worker, WorkerState::ReplacementPending);
        if !matches!(&*worker, WorkerState::Running(_)) {
            let running = platform::Worker::start(
                &self.0.program,
                &self.0.arguments,
                self.0.managed_python.as_ref(),
                self.0.output.clone(),
                |stop_handle| self.register_stop_handle(stop_handle),
            )?;
            *worker = WorkerState::Running(running);
            if replacing {
                self.0.output.push_restart_notice();
            }
        }
        let WorkerState::Running(running) = &mut *worker else {
            unreachable!("worker should be running");
        };
        let result = operation(running);
        if result.is_err() {
            *worker = WorkerState::ReplacementPending;
        }
        result
    }

    fn attach_output(&self, result: Result<SendResponse, SendFailure>) -> Response {
        let (captured_output, restart_notice) = self.0.output.take();
        let mut output = Response::default();
        output.push_text(captured_output);
        match result {
            Ok(response) => render_response(output, response, restart_notice),
            Err(SendFailure {
                output: worker_output,
                message,
            }) => {
                output.extend(worker_output);
                if output.is_empty() && restart_notice.is_empty() {
                    output.push_text(message);
                } else {
                    attach_error_output(&mut output, message, restart_notice);
                }
                output.is_error = true;
                output
            }
        }
    }

    fn shutdown_requested(&self) -> Result<bool, String> {
        self.0
            .shutdown_gate
            .lock()
            .map(|gate| matches!(*gate, ShutdownGate::Closed { .. }))
            .map_err(|_| "worker shutdown gate lock poisoned".to_string())
    }

    fn register_stop_handle(&self, handle: platform::StopHandle) -> Result<(), String> {
        let deadline = {
            let mut gate = self
                .0
                .shutdown_gate
                .lock()
                .map_err(|_| "worker shutdown gate lock poisoned".to_string())?;
            match &mut *gate {
                ShutdownGate::Open { stop_handle } => {
                    *stop_handle = Some(handle);
                    return Ok(());
                }
                ShutdownGate::Closed { deadline } => *deadline,
            }
        };
        handle.shutdown(deadline)?;
        Err("worker is shutting down".to_string())
    }

    fn close_shutdown_gate(
        &self,
        deadline: Instant,
    ) -> Result<Option<platform::StopHandle>, String> {
        let mut gate = self
            .0
            .shutdown_gate
            .lock()
            .map_err(|_| "worker shutdown gate lock poisoned".to_string())?;
        match std::mem::replace(&mut *gate, ShutdownGate::Closed { deadline }) {
            ShutdownGate::Open { stop_handle } => Ok(stop_handle),
            ShutdownGate::Closed { deadline } => {
                *gate = ShutdownGate::Closed { deadline };
                Ok(None)
            }
        }
    }

    /// Stops and reaps the worker, including one blocked in an evaluation.
    pub(crate) async fn shutdown(&self, deadline: Instant) -> Result<(), String> {
        let Some(stop_handle) = self.close_shutdown_gate(deadline)? else {
            return Ok(());
        };
        tokio::task::spawn_blocking(move || stop_handle.shutdown(deadline))
            .await
            .map_err(|error| format!("worker shutdown task failed: {error}"))?
    }
}

#[cfg(target_os = "macos")]
impl CapturedOutput {
    fn new() -> Self {
        Self(Arc::new(Mutex::new(CapturedOutputState::default())))
    }

    fn stream(&self) -> CapturedOutputStream {
        let mut state = self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let stream = state.streams.len();
        state.streams.push(Some(Vec::new()));
        CapturedOutputStream {
            output: self.clone(),
            stream,
        }
    }

    fn push_restart_notice(&self) {
        self.0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .restart_notice
            .push_str(WORKER_RESTART_BANNER);
    }

    fn take(&self) -> (String, String) {
        let mut state = self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let events = std::mem::take(&mut state.events);
        let restart_notice = std::mem::take(&mut state.restart_notice);
        let mut output = String::new();

        for event in events {
            match event {
                CapturedOutputEvent::Data { stream, bytes } => {
                    let pending = state.streams[stream]
                        .as_mut()
                        .expect("captured output stream should be open");
                    pending.extend_from_slice(&bytes);
                    let complete = complete_utf8_prefix(pending);
                    let incomplete = pending.split_off(complete);
                    let complete = std::mem::replace(pending, incomplete);
                    output.push_str(&String::from_utf8_lossy(&complete));
                }
                CapturedOutputEvent::Closed { stream } => {
                    let pending = state.streams[stream]
                        .take()
                        .expect("captured output stream should be open");
                    output.push_str(&String::from_utf8_lossy(&pending));
                }
            }
        }

        (output, restart_notice)
    }
}

#[cfg(not(target_os = "macos"))]
impl CapturedOutput {
    fn new() -> Self {
        Self
    }

    fn push_restart_notice(&self) {}

    fn take(&self) -> (String, String) {
        (String::new(), String::new())
    }
}

#[cfg(target_os = "macos")]
impl CapturedOutputStream {
    fn push(&self, bytes: &[u8]) {
        self.output
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .events
            .push(CapturedOutputEvent::Data {
                stream: self.stream,
                bytes: bytes.to_vec(),
            });
    }

    fn close(&self) {
        self.output
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .events
            .push(CapturedOutputEvent::Closed {
                stream: self.stream,
            });
    }
}

impl Evaluation {
    /// Queues bytes and briefly defers any outstanding input report for its receipt.
    fn submit_stdin(&self, stdin: String) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if stdin.is_empty() {
            return Ok(());
        }

        if let Some(report_at) = state.input_report_at.as_mut() {
            *report_at = Instant::now() + INPUT_REQUEST_GRACE;
        }
        let bytes = stdin.into_bytes();
        #[cfg(target_os = "macos")]
        if let Some(writer) = &state.stdin {
            writer.send(bytes)?;
            return Ok(());
        }
        state.pending_stdin.extend(bytes);
        Ok(())
    }

    #[cfg(target_os = "macos")]
    fn attach_writer(&self, writer: platform::StdinSender) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.stdin.is_some() {
            return Err("worker stdin was already attached to this evaluation".to_string());
        }
        if !state.pending_stdin.is_empty() {
            writer.send(std::mem::take(&mut state.pending_stdin))?;
        }
        state.stdin = Some(writer);
        Ok(())
    }

    #[cfg(target_os = "macos")]
    fn output(&self, output: String) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        state.output.push_text(output);
        Ok(())
    }

    #[cfg(target_os = "macos")]
    fn image(&self, data: String, mime_type: String) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        state.output.push_image(data, mime_type);
        Ok(())
    }

    #[cfg(target_os = "macos")]
    fn input_requested(&self, prompt: String) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.input_report_at.is_some() {
            return Err("worker requested new input before receiving prior input".to_string());
        }
        let prompt = serde_json::to_string(&prompt)
            .map_err(|error| format!("failed to render worker input prompt: {error}"))?;
        if !state.output.is_empty() && state.output.text_needs_newline() {
            state.output.push_text("\n");
        }
        state
            .output
            .push_text(format!("[input requested: {prompt}]\n"));
        state.input_report_at = Some(Instant::now() + INPUT_REQUEST_GRACE);
        self.changed.notify_one();
        Ok(())
    }

    #[cfg(target_os = "macos")]
    fn input_received(&self) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        state
            .input_report_at
            .take()
            .ok_or_else(|| "worker reported received input without requesting it".to_string())?;
        self.changed.notify_one();
        Ok(())
    }

    #[cfg(target_os = "macos")]
    fn input_complete(&self) -> Result<(), String> {
        let state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.input_report_at.is_some() {
            return Err("worker completed with an outstanding input request".to_string());
        }
        Ok(())
    }

    fn complete(&self, result: Result<(), String>) {
        let Ok(mut state) = self.state.lock() else {
            return;
        };
        state.input_report_at = None;
        let result = match result {
            Ok(()) => Ok(std::mem::take(&mut state.output)),
            Err(message) => Err(SendFailure {
                output: std::mem::take(&mut state.output),
                message,
            }),
        };
        state.result = Some(result);
        self.changed.notify_one();
    }

    async fn wait(&self, timeout: Duration) -> Result<EvaluationWait, String> {
        let started = Instant::now();
        loop {
            let changed = self.changed.notified();
            let grace = match self.reported_state(false)? {
                EvaluationStatus::Waiting => None,
                EvaluationStatus::Grace(grace) => Some(grace),
                EvaluationStatus::Report(state) => return Ok(state),
            };
            let remaining = timeout.saturating_sub(started.elapsed());
            if remaining.is_zero() {
                return self.state_at_deadline();
            }
            let wait = grace.map_or(remaining, |grace| grace.min(remaining));
            if tokio::time::timeout(wait, changed).await.is_err() {
                if grace.is_some_and(|grace| grace <= remaining) {
                    continue;
                }
                return self.state_at_deadline();
            }
        }
    }

    fn state_at_deadline(&self) -> Result<EvaluationWait, String> {
        match self.reported_state(true)? {
            EvaluationStatus::Report(state) => Ok(state),
            EvaluationStatus::Waiting | EvaluationStatus::Grace(_) => {
                unreachable!("the deadline makes every evaluation state reportable")
            }
        }
    }

    fn reported_state(&self, at_deadline: bool) -> Result<EvaluationStatus, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if let Some(result) = state.result.take() {
            return Ok(EvaluationStatus::Report(EvaluationWait::Completed(result)));
        }
        let Some(report_at) = state.input_report_at else {
            return if at_deadline {
                Ok(EvaluationStatus::Report(EvaluationWait::Running))
            } else {
                Ok(EvaluationStatus::Waiting)
            };
        };
        let grace = report_at.saturating_duration_since(Instant::now());
        if !at_deadline && !grace.is_zero() {
            return Ok(EvaluationStatus::Grace(grace));
        }
        let output = std::mem::take(&mut state.output);
        Ok(EvaluationStatus::Report(EvaluationWait::InputRequested(
            output,
        )))
    }
}

fn render_response(
    mut output: Response,
    response: SendResponse,
    restart_notice: String,
) -> Response {
    match response {
        SendResponse::Completed(completed) => {
            output.extend(completed);
            append_restart_notice(&mut output, &restart_notice);
            if output.is_empty() {
                output.push_text("[done]");
            }
            output
        }
        SendResponse::InputRequested(input) => {
            output.extend(input);
            append_input_banner(&mut output, &restart_notice);
            output
        }
        SendResponse::Running => {
            append_state_banner(&mut output, &restart_notice, "[running]");
            output
        }
        SendResponse::Idle => {
            append_state_banner(&mut output, &restart_notice, "[idle]");
            output
        }
    }
}

fn append_input_banner(output: &mut Response, restart_notice: &str) {
    if !append_restart_notice(output, restart_notice) && output.text_needs_newline() {
        output.push_text("\n");
    }
    output.push_text("[stdin needed]");
}

fn append_state_banner(output: &mut Response, restart_notice: &str, banner: &str) {
    if !append_restart_notice(output, restart_notice) {
        output.push_text("\n");
    }
    output.push_text(banner);
}

fn append_restart_notice(output: &mut Response, restart_notice: &str) -> bool {
    if restart_notice.is_empty() {
        return false;
    }
    if output.text_needs_newline() {
        output.push_text("\n");
    }
    output.push_text(restart_notice);
    true
}

fn attach_error_output(output: &mut Response, error: String, restart_notice: String) {
    if !output.is_empty() && output.text_needs_newline() {
        output.push_text("\n");
    }
    output.push_text(format!("[{error}]"));
    append_restart_notice(output, &restart_notice);
}

#[cfg(target_os = "macos")]
fn complete_utf8_prefix(bytes: &[u8]) -> usize {
    let mut offset = 0;
    loop {
        match std::str::from_utf8(&bytes[offset..]) {
            Ok(_) => return bytes.len(),
            Err(error) => match error.error_len() {
                Some(length) => offset += error.valid_up_to() + length,
                None => return offset + error.valid_up_to(),
            },
        }
    }
}

#[cfg(target_os = "macos")]
mod platform {
    use std::ffi::OsString;
    use std::io::{Read, Write};
    use std::path::Path;
    use std::process::Stdio;
    use std::sync::{Arc, Mutex, mpsc};
    use std::thread;
    use std::time::Instant;

    use crate::worker_protocol::{ServerMessage, WorkerMessage};

    pub(super) struct Worker {
        reader: crate::sideband::Reader,
        stop_handle: StopHandle,
    }

    #[derive(Clone)]
    pub(super) struct StopHandle {
        writer: crate::sideband::Writer,
        stdin: StdinSender,
        child: Arc<Mutex<crate::sandbox::SandboxedChild>>,
    }

    #[derive(Clone)]
    pub(super) struct StdinSender(mpsc::Sender<StdinMessage>);

    enum StdinMessage {
        Write(Vec<u8>),
        Close,
    }

    impl Worker {
        /// Starts an executable worker and waits for its ready message.
        pub(super) fn start(
            program: &Path,
            arguments: &[OsString],
            managed_python: Option<&crate::python::Managed>,
            output: super::CapturedOutput,
            on_started: impl FnOnce(StopHandle) -> Result<(), String>,
        ) -> Result<Self, String> {
            let (reader, writer, child_fds) = crate::sideband::bind()
                .map_err(|error| format!("failed to create worker sideband: {error}"))?;
            let mut command = crate::sandbox::SandboxedCommand::new(program.as_os_str())
                .map_err(|error| format!("failed to prepare worker sandbox: {error}"))?;
            if let Some(managed_python) = managed_python {
                managed_python.configure_worker(&mut command);
            }
            command
                .args(arguments)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .new_process_group();
            child_fds.configure(&mut command);
            let mut child = command
                .spawn()
                .map_err(|error| format!("failed to launch worker: {error}"))?;
            drop(child_fds);
            let stdin = child
                .take_stdin()
                .expect("piped worker stdin should be available");
            let stdout = child
                .take_stdout()
                .expect("piped worker stdout should be available");
            let stderr = child
                .take_stderr()
                .expect("piped worker stderr should be available");
            start_output_reader(stdout, output.stream());
            start_output_reader(stderr, output.stream());
            let child = Arc::new(Mutex::new(child));
            let stdin = start_stdin_writer(stdin, child.clone());

            let stop_handle = StopHandle {
                writer,
                stdin,
                child,
            };
            let mut worker = Self {
                reader,
                stop_handle,
            };
            on_started(worker.stop_handle.clone())?;
            match worker.receive()? {
                WorkerMessage::Ready => {}
                WorkerMessage::Output { data } => {
                    return Err(format!("worker emitted output before readiness: {data}"));
                }
                WorkerMessage::Image { .. } => {
                    return Err("worker emitted an image before readiness".to_string());
                }
                _ => return Err("worker did not report readiness".to_string()),
            }
            Ok(worker)
        }

        /// Sends one cell and collects output until the completed message.
        pub(super) fn evaluate(
            &mut self,
            cell: crate::cell::Cell,
            evaluation: &super::Evaluation,
        ) -> Result<(), String> {
            let crate::cell::Cell { language, source } = cell;
            self.stop_handle
                .writer
                .send(&ServerMessage::Evaluate { language, source })
                .map_err(|error| format!("worker sideband write failed: {error}"))?;
            evaluation.attach_writer(self.stop_handle.stdin.clone())?;

            loop {
                match self.receive()? {
                    WorkerMessage::Output { data } => evaluation.output(data)?,
                    WorkerMessage::Image { data, mime_type } => {
                        evaluation.image(data, mime_type)?;
                    }
                    WorkerMessage::InputRequested { prompt } => {
                        evaluation.input_requested(prompt)?;
                    }
                    WorkerMessage::InputReceived => evaluation.input_received()?,
                    WorkerMessage::Completed => {
                        evaluation.input_complete()?;
                        return Ok(());
                    }
                    WorkerMessage::Ready => {
                        return Err("worker sent an unexpected ready message".to_string());
                    }
                }
            }
        }

        fn receive(&mut self) -> Result<WorkerMessage, String> {
            self.reader
                .receive()
                .map_err(|error| format!("worker sideband read failed: {error}"))
        }

        pub(super) fn write_stdin(&self, stdin: String) -> Result<(), String> {
            self.stop_handle.stdin.send(stdin.into_bytes())
        }
    }

    fn start_output_reader(
        mut stream: impl Read + Send + 'static,
        output: super::CapturedOutputStream,
    ) {
        let _ = thread::spawn(move || {
            let mut buffer = [0; 8 * 1024];
            loop {
                match stream.read(&mut buffer) {
                    Ok(0) => {
                        output.close();
                        return;
                    }
                    Ok(length) => {
                        output.push(&buffer[..length]);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(_) => {
                        output.close();
                        return;
                    }
                }
            }
        });
    }

    fn start_stdin_writer(
        mut stdin: std::process::ChildStdin,
        child: Arc<Mutex<crate::sandbox::SandboxedChild>>,
    ) -> StdinSender {
        let (sender, receiver) = mpsc::channel();
        let _ = thread::spawn(move || {
            for message in receiver {
                match message {
                    StdinMessage::Write(bytes) => {
                        if stdin.write_all(&bytes).is_err() {
                            if let Ok(mut child) = child.lock() {
                                let _ = child.force_stop();
                            }
                            return;
                        }
                    }
                    StdinMessage::Close => return,
                }
            }
        });
        StdinSender(sender)
    }

    impl StdinSender {
        pub(super) fn send(&self, bytes: Vec<u8>) -> Result<(), String> {
            self.0
                .send(StdinMessage::Write(bytes))
                .map_err(|_| "worker stdin writer stopped".to_string())
        }

        fn close(&self) {
            let _ = self.0.send(StdinMessage::Close);
        }
    }

    impl Drop for Worker {
        fn drop(&mut self) {
            let _ = self.stop_handle.force_stop();
        }
    }

    impl StopHandle {
        /// Attempts graceful shutdown while independently enforcing its deadline.
        pub(super) fn shutdown(&self, deadline: Instant) -> Result<(), String> {
            let writer = self.writer.clone();
            let stdin = self.stdin.clone();
            let _ = thread::spawn(move || {
                stdin.close();
                let _ = writer.send(&ServerMessage::Shutdown);
            });

            let mut child = self
                .child
                .lock()
                .map_err(|_| "worker child lock poisoned".to_string())?;
            let remaining = deadline.saturating_duration_since(Instant::now());
            if child.wait_timeout(remaining)?.is_none() {
                child.force_stop()?;
            }
            Ok(())
        }

        fn force_stop(&self) -> Result<(), String> {
            self.child
                .lock()
                .map_err(|_| "worker child lock poisoned".to_string())?
                .force_stop()
        }
    }
}

#[cfg(not(target_os = "macos"))]
mod platform {
    use std::ffi::OsString;
    use std::path::Path;

    pub(super) struct Worker;

    #[derive(Clone)]
    pub(super) struct StopHandle;

    impl Worker {
        pub(super) fn start(
            _program: &Path,
            _arguments: &[OsString],
            _managed_python: Option<&crate::python::Managed>,
            _output: super::CapturedOutput,
            _on_started: impl FnOnce(StopHandle) -> Result<(), String>,
        ) -> Result<Self, String> {
            Err("workers are supported only on macOS".to_string())
        }

        pub(super) fn evaluate(
            &mut self,
            cell: crate::cell::Cell,
            _evaluation: &super::Evaluation,
        ) -> Result<(), String> {
            let crate::cell::Cell { language, source } = cell;
            let _ = (language, source);
            unreachable!("unsupported workers cannot start")
        }

        pub(super) fn write_stdin(&self, _stdin: String) -> Result<(), String> {
            unreachable!("unsupported workers cannot start")
        }
    }

    impl StopHandle {
        pub(super) fn shutdown(&self, _deadline: std::time::Instant) -> Result<(), String> {
            Ok(())
        }
    }
}
