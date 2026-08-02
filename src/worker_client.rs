use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const INPUT_REQUEST_GRACE: Duration = Duration::from_millis(10);

/// A cloneable handle to one lazily started worker.
#[derive(Clone)]
pub(crate) struct Client(Arc<ClientInner>);

struct ClientInner {
    program: PathBuf,
    arguments: Vec<OsString>,
    worker: Mutex<Option<platform::Worker>>,
    evaluation: Mutex<Option<Arc<Evaluation>>>,
    output: CapturedOutput,
    shutdown_gate: Mutex<ShutdownGate>,
}

#[derive(Clone, Default)]
struct CapturedOutput(Arc<Mutex<String>>);

struct Evaluation {
    state: Mutex<EvaluationState>,
    changed: tokio::sync::Notify,
}

struct EvaluationState {
    result: Option<Result<String, String>>,
    output: String,
    input_report_at: Option<Instant>,
    #[cfg(target_os = "macos")]
    stdin: Option<platform::StdinSender>,
    pending_stdin: Vec<u8>,
}

enum EvaluationWait {
    Running,
    InputRequested(String),
    Completed(Result<String, String>),
}

enum SendResponse {
    Idle,
    Running,
    InputRequested(String),
    Completed(String),
}

enum EvaluationStatus {
    Waiting,
    Grace(Duration),
    Report(EvaluationWait),
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
        Self::with_arguments(program, Vec::new())
    }

    pub(crate) fn r() -> Result<Self, String> {
        let program = std::env::current_exe()
            .map_err(|error| format!("failed to locate the R worker executable: {error}"))?;
        Ok(Self::with_arguments(
            program,
            vec![OsString::from("worker")],
        ))
    }

    fn with_arguments(program: PathBuf, arguments: Vec<OsString>) -> Self {
        Self(Arc::new(ClientInner {
            program,
            arguments,
            worker: Mutex::new(None),
            evaluation: Mutex::new(None),
            output: CapturedOutput::default(),
            shutdown_gate: Mutex::new(ShutdownGate::Open { stop_handle: None }),
        }))
    }

    /// Starts one cell, supplies its stdin, or polls the cell already running.
    pub(crate) async fn send(
        &self,
        r: Option<String>,
        stdin: Option<String>,
        timeout: Duration,
    ) -> Result<String, String> {
        let result = self.send_inner(r, stdin, timeout).await;
        self.attach_output(result)
    }

    async fn send_inner(
        &self,
        r: Option<String>,
        stdin: Option<String>,
        timeout: Duration,
    ) -> Result<SendResponse, String> {
        let evaluation = match r {
            Some(r) => self.start_evaluation(r, stdin)?,
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
        r: String,
        stdin: Option<String>,
    ) -> Result<Arc<Evaluation>, String> {
        if self.shutdown_requested()? {
            return Err("worker is shutting down".to_string());
        }

        let evaluation = Arc::new(Evaluation {
            state: Mutex::new(EvaluationState {
                result: None,
                output: String::new(),
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
            return Err("worker is already evaluating a cell; poll without `r`".to_string());
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
            tokio::task::spawn_blocking(move || client.evaluate_blocking(r, &evaluator));
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

    fn evaluate_blocking(&self, r: String, evaluation: &Evaluation) -> Result<(), String> {
        self.with_worker(|worker| worker.evaluate(r, evaluation))
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

        if worker.is_none() {
            *worker = Some(platform::Worker::start(
                &self.0.program,
                &self.0.arguments,
                self.0.output.clone(),
                |stop_handle| self.register_stop_handle(stop_handle),
            )?);
        }
        let result = operation(worker.as_mut().expect("worker should be running"));
        if result.is_err() {
            *worker = None;
        }
        result
    }

    fn attach_output(&self, result: Result<SendResponse, String>) -> Result<String, String> {
        let output = self.0.output.take();
        match result {
            Ok(response) => Ok(render_response(output, response)),
            Err(error) if output.is_empty() => Err(error),
            Err(error) => Err(attach_error_output(output, error)),
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

impl CapturedOutput {
    #[cfg(target_os = "macos")]
    fn push(&self, text: &str) {
        self.0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push_str(text);
    }

    fn take(&self) -> String {
        let mut output = self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        std::mem::take(&mut *output)
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
        state.output.push_str(&output);
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
        state.output.push_str(prompt.trim_end());
        append_newline(&mut state.output);
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
        let result = result.map(|()| std::mem::take(&mut state.output));
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

fn append_newline(output: &mut String) {
    if !output.is_empty() && !output.ends_with('\n') {
        output.push('\n');
    }
}

fn render_response(mut output: String, response: SendResponse) -> String {
    match response {
        SendResponse::Completed(completed) => {
            output.push_str(&completed);
            if output.is_empty() {
                "[done]".to_string()
            } else {
                output
            }
        }
        SendResponse::InputRequested(input) => {
            output.push_str(&input);
            append_newline(&mut output);
            output.push_str("[input]");
            output
        }
        SendResponse::Running => {
            append_newline(&mut output);
            output.push_str("[running]");
            output
        }
        SendResponse::Idle => {
            append_newline(&mut output);
            output.push_str("[idle]");
            output
        }
    }
}

fn attach_error_output(mut output: String, error: String) -> String {
    append_newline(&mut output);
    output.push_str(&error);
    output
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
            output: super::CapturedOutput,
            on_started: impl FnOnce(StopHandle) -> Result<(), String>,
        ) -> Result<Self, String> {
            let (reader, writer, child_fds) = crate::sideband::bind()
                .map_err(|error| format!("failed to create worker sideband: {error}"))?;
            let mut command = crate::sandbox::SandboxedCommand::new(program.as_os_str())
                .map_err(|error| format!("failed to prepare worker sandbox: {error}"))?;
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
            start_output_reader(stdout, output.clone());
            start_output_reader(stderr, output);
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
            if !matches!(worker.receive()?, WorkerMessage::Ready) {
                return Err("worker did not report readiness".to_string());
            }
            Ok(worker)
        }

        /// Sends one cell and collects output until the completed message.
        pub(super) fn evaluate(
            &mut self,
            r: String,
            evaluation: &super::Evaluation,
        ) -> Result<(), String> {
            self.stop_handle
                .writer
                .send(&ServerMessage::Evaluate { r })
                .map_err(|error| format!("worker sideband write failed: {error}"))?;
            evaluation.attach_writer(self.stop_handle.stdin.clone())?;

            loop {
                match self.receive()? {
                    WorkerMessage::Output { data } => evaluation.output(data)?,
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

    fn start_output_reader(mut stream: impl Read + Send + 'static, output: super::CapturedOutput) {
        let _ = thread::spawn(move || {
            let mut pending = Vec::new();
            let mut buffer = [0; 8 * 1024];
            loop {
                match stream.read(&mut buffer) {
                    Ok(0) => {
                        output.push(&String::from_utf8_lossy(&pending));
                        return;
                    }
                    Ok(length) => {
                        pending.extend_from_slice(&buffer[..length]);
                        let complete = complete_utf8_prefix(&pending);
                        let remainder = pending.split_off(complete);
                        output.push(&String::from_utf8_lossy(&pending));
                        pending = remainder;
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(_) => {
                        output.push(&String::from_utf8_lossy(&pending));
                        return;
                    }
                }
            }
        });
    }

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
            _output: super::CapturedOutput,
            _on_started: impl FnOnce(StopHandle) -> Result<(), String>,
        ) -> Result<Self, String> {
            Err("workers are supported only on macOS".to_string())
        }

        pub(super) fn evaluate(
            &mut self,
            _r: String,
            _evaluation: &super::Evaluation,
        ) -> Result<(), String> {
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
