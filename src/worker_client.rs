use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// A cloneable handle to one lazily started worker.
#[derive(Clone)]
pub(crate) struct Client(Arc<ClientInner>);

struct ClientInner {
    program: PathBuf,
    arguments: Vec<OsString>,
    worker: Mutex<Option<platform::Worker>>,
    evaluation: Mutex<Option<Arc<Evaluation>>>,
    shutdown_gate: Mutex<ShutdownGate>,
}

struct Evaluation {
    state: Mutex<EvaluationState>,
    changed: tokio::sync::Notify,
}

struct EvaluationState {
    result: Option<Result<String, String>>,
    input_request: Option<InputBoundary>,
    #[cfg(target_os = "macos")]
    stdin: Option<platform::StdinSender>,
    pending_stdin: Vec<u8>,
}

struct InputBoundary {
    output: String,
    delivered: bool,
}

enum EvaluationWait {
    Running,
    InputRequested(String),
    Completed(Result<String, String>),
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
        let (evaluation, defer_request) = match (r, stdin) {
            (Some(r), stdin) => self.start_evaluation(r, stdin)?,
            (None, Some(stdin)) => match self.current_evaluation()? {
                Some(evaluation) => {
                    let defer_request = evaluation.submit_stdin(stdin)?;
                    (evaluation, defer_request)
                }
                None => {
                    return Err("stdin is accepted only while an R cell is active".to_string());
                }
            },
            (None, None) => match self.current_evaluation()? {
                Some(evaluation) => (evaluation, false),
                None => return Ok("[idle]".to_string()),
            },
        };

        match evaluation.wait(timeout, defer_request).await? {
            EvaluationWait::Running => Ok("[running]".to_string()),
            EvaluationWait::InputRequested(output) => Ok(output),
            EvaluationWait::Completed(result) => {
                self.clear_evaluation(&evaluation)?;
                result
            }
        }
    }

    fn start_evaluation(
        &self,
        r: String,
        stdin: Option<String>,
    ) -> Result<(Arc<Evaluation>, bool), String> {
        if self.shutdown_requested()? {
            return Err("worker is shutting down".to_string());
        }

        let evaluation = Arc::new(Evaluation {
            state: Mutex::new(EvaluationState {
                result: None,
                input_request: None,
                #[cfg(target_os = "macos")]
                stdin: None,
                pending_stdin: Vec::new(),
            }),
            changed: tokio::sync::Notify::new(),
        });
        let defer_request = match stdin {
            Some(stdin) => evaluation.submit_stdin(stdin)?,
            None => false,
        };

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
        Ok((evaluation, defer_request))
    }

    fn current_evaluation(&self) -> Result<Option<Arc<Evaluation>>, String> {
        self.0
            .evaluation
            .lock()
            .map(|evaluation| evaluation.clone())
            .map_err(|_| "worker evaluation lock poisoned".to_string())
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

    fn evaluate_blocking(&self, r: String, evaluation: &Evaluation) -> Result<String, String> {
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
                |stop_handle| self.register_stop_handle(stop_handle),
            )?);
        }
        let result = worker
            .as_mut()
            .expect("worker should be running")
            .evaluate(r, evaluation);
        if result.is_err() {
            *worker = None;
        }
        result
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

impl Evaluation {
    /// Queues bytes and reports whether this call supplied nonempty stdin
    /// without replying to an input boundary already exposed to the caller.
    fn submit_stdin(&self, stdin: String) -> Result<bool, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.result.is_some() {
            return Err(
                "the R cell has completed; poll its result before sending stdin".to_string(),
            );
        }
        if stdin.is_empty() {
            return Ok(false);
        }

        let defer_request = state.input_request.take().is_none();
        let bytes = stdin.into_bytes();
        #[cfg(target_os = "macos")]
        if let Some(writer) = &state.stdin {
            writer.send(bytes)?;
            return Ok(defer_request);
        }
        state.pending_stdin.extend(bytes);
        Ok(defer_request)
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
    fn input_requested(&self, output: &mut String, prompt: String) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        output.push_str(prompt.trim_end());
        let mut boundary = InputBoundary {
            output: std::mem::take(output),
            delivered: false,
        };
        if let Some(previous) = state.input_request.take()
            && !previous.delivered
        {
            let mut combined = previous.output;
            append_newline(&mut combined);
            combined.push_str(&boundary.output);
            boundary.output = combined;
        }
        state.input_request = Some(boundary);
        self.changed.notify_one();
        Ok(())
    }

    fn complete(&self, mut result: Result<String, String>) {
        let Ok(mut state) = self.state.lock() else {
            return;
        };
        if let Some(boundary) = state.input_request.take()
            && !boundary.delivered
            && let Ok(output) = &mut result
        {
            let mut combined = boundary.output;
            append_newline(&mut combined);
            combined.push_str(output);
            *output = combined;
        }
        state.result = Some(result);
        self.changed.notify_one();
    }

    async fn wait(&self, timeout: Duration, defer_request: bool) -> Result<EvaluationWait, String> {
        let deadline = tokio::time::Instant::now() + timeout;
        loop {
            let changed = self.changed.notified();
            if let Some(event) = self.take_event(!defer_request)? {
                return Ok(event);
            }
            if tokio::time::timeout_at(deadline, changed).await.is_err() {
                return Ok(self.take_event(true)?.unwrap_or(EvaluationWait::Running));
            }
        }
    }

    fn take_event(&self, include_request: bool) -> Result<Option<EvaluationWait>, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if let Some(result) = state.result.take() {
            return Ok(Some(EvaluationWait::Completed(result)));
        }
        if !include_request {
            return Ok(None);
        }
        let Some(boundary) = state.input_request.as_mut() else {
            return Ok(None);
        };
        let output = if boundary.delivered {
            "[input]".to_string()
        } else {
            boundary.delivered = true;
            let mut output = boundary.output.clone();
            append_newline(&mut output);
            output.push_str("[input]");
            output
        };
        Ok(Some(EvaluationWait::InputRequested(output)))
    }
}

fn append_newline(output: &mut String) {
    if !output.is_empty() && !output.ends_with('\n') {
        output.push('\n');
    }
}

#[cfg(target_os = "macos")]
mod platform {
    use std::ffi::OsString;
    use std::io::Write;
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
            on_started: impl FnOnce(StopHandle) -> Result<(), String>,
        ) -> Result<Self, String> {
            let (reader, writer, child_fds) = crate::sideband::bind()
                .map_err(|error| format!("failed to create worker sideband: {error}"))?;
            let mut command = crate::sandbox::SandboxedCommand::new(program.as_os_str())
                .map_err(|error| format!("failed to prepare worker sandbox: {error}"))?;
            command
                .args(arguments)
                .stdin(Stdio::piped())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .new_process_group();
            child_fds.configure(&mut command);
            let mut child = command
                .spawn()
                .map_err(|error| format!("failed to launch worker: {error}"))?;
            drop(child_fds);
            let stdin = child
                .take_stdin()
                .expect("piped worker stdin should be available");
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
        ) -> Result<String, String> {
            self.stop_handle
                .writer
                .send(&ServerMessage::Evaluate { r })
                .map_err(|error| format!("worker sideband write failed: {error}"))?;
            evaluation.attach_writer(self.stop_handle.stdin.clone())?;

            let mut output = String::new();
            loop {
                match self.receive()? {
                    WorkerMessage::Output { data } => output.push_str(&data),
                    WorkerMessage::InputRequested { prompt } => {
                        evaluation.input_requested(&mut output, prompt)?;
                    }
                    WorkerMessage::Completed => {
                        if output.is_empty() {
                            output.push_str("[done]");
                        }
                        return Ok(output);
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
            _on_started: impl FnOnce(StopHandle) -> Result<(), String>,
        ) -> Result<Self, String> {
            Err("workers are supported only on macOS".to_string())
        }

        pub(super) fn evaluate(
            &mut self,
            _r: String,
            _evaluation: &super::Evaluation,
        ) -> Result<String, String> {
            unreachable!("unsupported workers cannot start")
        }
    }

    impl StopHandle {
        pub(super) fn shutdown(&self, _deadline: std::time::Instant) -> Result<(), String> {
            Ok(())
        }
    }
}
