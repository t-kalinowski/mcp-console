use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Instant;

/// A cloneable handle to one lazily started worker.
#[derive(Clone)]
pub(crate) struct Client(Arc<ClientInner>);

struct ClientInner {
    program: PathBuf,
    arguments: Vec<OsString>,
    state: Mutex<WorkerState>,
    shutdown_gate: Mutex<ShutdownGate>,
}

enum WorkerState {
    Cold,
    Idle(platform::Worker),
    InputRequired(platform::Worker),
}

enum Operation {
    Evaluate { r: String, stdin: Option<String> },
    Input(String),
}

struct Boundary {
    output: String,
    input_required: bool,
}

struct OperationFailure {
    message: String,
    input_required: bool,
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
            state: Mutex::new(WorkerState::Cold),
            shutdown_gate: Mutex::new(ShutdownGate::Open { stop_handle: None }),
        }))
    }

    /// Evaluates one cell without blocking the async MCP server runtime.
    ///
    /// The worker starts on the first call and is then reused by later calls.
    /// `spawn_blocking` owns a cheap handle clone, not a copy of the process.
    pub(crate) async fn evaluate(
        &self,
        r: String,
        stdin: Option<String>,
    ) -> Result<String, String> {
        self.run(Operation::Evaluate { r, stdin }).await
    }

    /// Supplies exact input after the worker requests it.
    pub(crate) async fn provide_input(&self, stdin: String) -> Result<String, String> {
        self.run(Operation::Input(stdin)).await
    }

    async fn run(&self, operation: Operation) -> Result<String, String> {
        let client = self.clone();
        tokio::task::spawn_blocking(move || client.run_blocking(operation))
            .await
            .map_err(|error| format!("worker task failed: {error}"))?
    }

    fn run_blocking(&self, operation: Operation) -> Result<String, String> {
        if self.shutdown_requested()? {
            return Err("worker is shutting down".to_string());
        }

        let mut state = self
            .0
            .state
            .lock()
            .map_err(|_| "worker state lock poisoned".to_string())?;
        if self.shutdown_requested()? {
            return Err("worker is shutting down".to_string());
        }

        let current = std::mem::replace(&mut *state, WorkerState::Cold);
        let (mut worker, operation) = match (operation, current) {
            (operation @ Operation::Evaluate { .. }, WorkerState::Cold) => {
                let worker =
                    platform::Worker::start(&self.0.program, &self.0.arguments, |stop_handle| {
                        self.register_stop_handle(stop_handle)
                    })?;
                (worker, operation)
            }
            (operation @ Operation::Evaluate { .. }, WorkerState::Idle(worker)) => {
                (worker, operation)
            }
            (Operation::Evaluate { .. }, WorkerState::InputRequired(worker)) => {
                *state = WorkerState::InputRequired(worker);
                return Err(
                    "cannot evaluate R code while the session is waiting for stdin".to_string(),
                );
            }
            (Operation::Input(_), WorkerState::Cold) => {
                *state = WorkerState::Cold;
                return Err("stdin is accepted only at an R input prompt".to_string());
            }
            (Operation::Input(_), WorkerState::Idle(worker)) => {
                *state = WorkerState::Idle(worker);
                return Err("stdin is accepted only at an R input prompt".to_string());
            }
            (Operation::Input(stdin), WorkerState::InputRequired(worker)) => {
                (worker, Operation::Input(stdin))
            }
        };

        let result = match operation {
            Operation::Evaluate { r, stdin } => worker.evaluate(r, stdin),
            Operation::Input(stdin) => worker.provide_input(stdin),
        };
        match result {
            Ok(boundary) => {
                *state = if boundary.input_required {
                    WorkerState::InputRequired(worker)
                } else {
                    WorkerState::Idle(worker)
                };
                Ok(boundary.output)
            }
            Err(error) => {
                if error.input_required {
                    *state = WorkerState::InputRequired(worker);
                }
                Err(error.message)
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
mod platform {
    use std::ffi::OsString;
    use std::io::Write;
    use std::path::Path;
    use std::process::{ChildStdin, Stdio};
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::Instant;

    use crate::worker_protocol::{ServerMessage, WorkerMessage};

    // POSIX's minimum atomic pipe write and macOS's reported PIPE_BUF.
    const STDIN_LINE_BYTES: usize = 512;

    fn worker_failure(message: String) -> super::OperationFailure {
        super::OperationFailure {
            message,
            input_required: false,
        }
    }

    fn input_rejected(message: String) -> super::OperationFailure {
        super::OperationFailure {
            message,
            input_required: true,
        }
    }

    pub(super) struct Worker {
        reader: crate::sideband::Reader,
        stdin: WorkerStdin,
        input_line_bytes: usize,
        stop_handle: StopHandle,
    }

    #[derive(Clone)]
    pub(super) struct StopHandle {
        writer: crate::sideband::Writer,
        stdin: WorkerStdin,
        child: Arc<Mutex<crate::sandbox::SandboxedChild>>,
    }

    #[derive(Clone)]
    struct WorkerStdin(Arc<Mutex<Option<ChildStdin>>>);

    struct PendingInput {
        bytes: Vec<u8>,
        offset: usize,
        requested: bool,
        line_bytes: usize,
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
            let stdin = WorkerStdin(Arc::new(Mutex::new(Some(
                child
                    .take_stdin()
                    .expect("piped worker stdin should be available"),
            ))));

            let stop_handle = StopHandle {
                writer,
                stdin: stdin.clone(),
                child: Arc::new(Mutex::new(child)),
            };
            let mut worker = Self {
                reader,
                stdin,
                input_line_bytes: 0,
                stop_handle,
            };
            on_started(worker.stop_handle.clone())?;
            if !matches!(worker.receive()?, WorkerMessage::Ready) {
                return Err("worker did not report readiness".to_string());
            }
            Ok(worker)
        }

        /// Sends one cell and collects output until completion or an input boundary.
        pub(super) fn evaluate(
            &mut self,
            r: String,
            stdin: Option<String>,
        ) -> Result<super::Boundary, super::OperationFailure> {
            self.input_line_bytes = 0;
            self.stop_handle
                .writer
                .send(&ServerMessage::Evaluate { r })
                .map_err(|error| {
                    worker_failure(format!("worker sideband write failed: {error}"))
                })?;
            self.read_boundary(stdin.map(|stdin| PendingInput::new(stdin, 0)))
        }

        pub(super) fn provide_input(
            &mut self,
            stdin: String,
        ) -> Result<super::Boundary, super::OperationFailure> {
            let mut pending = PendingInput::new(stdin, self.input_line_bytes);
            let Some(line) = pending.next_line().map_err(input_rejected)? else {
                return Ok(super::Boundary {
                    output: "[input]".to_string(),
                    input_required: true,
                });
            };
            self.stdin.write(line).map_err(worker_failure)?;
            self.input_line_bytes = pending.line_bytes;
            self.read_boundary(Some(pending))
        }

        fn write_pending_input(
            &mut self,
            pending: &mut PendingInput,
        ) -> Result<bool, super::OperationFailure> {
            let Some(line) = pending.next_line().map_err(input_rejected)? else {
                return Ok(false);
            };
            self.stdin.write(line).map_err(worker_failure)?;
            self.input_line_bytes = pending.line_bytes;
            Ok(true)
        }

        fn read_boundary(
            &mut self,
            mut pending_stdin: Option<PendingInput>,
        ) -> Result<super::Boundary, super::OperationFailure> {
            let mut output = String::new();
            loop {
                match self.receive().map_err(worker_failure)? {
                    WorkerMessage::Output { data } => output.push_str(&data),
                    WorkerMessage::InputRequested { prompt } => {
                        if let Some(pending) = pending_stdin.as_mut()
                            && self.write_pending_input(pending)?
                        {
                            append_text(&mut output, prompt.trim_end());
                            append_newline(&mut output);
                            continue;
                        }

                        append_text(&mut output, prompt.trim_end());
                        append_marker(&mut output, "[input]");
                        return Ok(super::Boundary {
                            output,
                            input_required: true,
                        });
                    }
                    WorkerMessage::Completed => {
                        self.input_line_bytes = 0;
                        if output.is_empty() {
                            output.push_str("[done]");
                        }
                        if pending_stdin
                            .as_ref()
                            .is_some_and(|pending| !pending.requested)
                        {
                            append_marker(&mut output, "[stdin discarded]");
                        }
                        return Ok(super::Boundary {
                            output,
                            input_required: false,
                        });
                    }
                    WorkerMessage::Ready => {
                        return Err(worker_failure(
                            "worker sent an unexpected ready message".to_string(),
                        ));
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

    fn append_text(output: &mut String, text: &str) {
        if !text.is_empty() {
            output.push_str(text);
        }
    }

    fn append_newline(output: &mut String) {
        if !output.is_empty() && !output.ends_with('\n') {
            output.push('\n');
        }
    }

    fn append_marker(output: &mut String, marker: &str) {
        append_newline(output);
        output.push_str(marker);
    }

    impl Drop for Worker {
        fn drop(&mut self) {
            let _ = self.stdin.close();
            let _ = self.stop_handle.force_stop();
        }
    }

    impl WorkerStdin {
        fn write(&self, input: &[u8]) -> Result<(), String> {
            self.0
                .lock()
                .map_err(|_| "worker stdin lock poisoned".to_string())?
                .as_mut()
                .ok_or_else(|| "worker stdin is closed".to_string())?
                .write_all(input)
                .map_err(|error| format!("worker stdin write failed: {error}"))
        }

        fn close(&self) -> Result<(), String> {
            self.0
                .lock()
                .map_err(|_| "worker stdin lock poisoned".to_string())?
                .take();
            Ok(())
        }
    }

    impl PendingInput {
        fn new(input: String, line_bytes: usize) -> Self {
            Self {
                bytes: input.into_bytes(),
                offset: 0,
                requested: false,
                line_bytes,
            }
        }

        fn next_line(&mut self) -> Result<Option<&[u8]>, String> {
            if !self.requested {
                self.requested = true;
                if self.bytes.contains(&0) {
                    return Err("stdin cannot contain NUL".to_string());
                }
            }
            if self.offset == self.bytes.len() {
                return Ok(None);
            }

            let start = self.offset;
            let newline = self.bytes[start..].iter().position(|byte| *byte == b'\n');
            let length = newline.map_or(self.bytes.len() - start, |newline| newline + 1);
            if self.line_bytes + length > STDIN_LINE_BYTES {
                return Err("stdin lines cannot exceed 512 bytes including the newline".to_string());
            }
            self.offset += length;
            self.line_bytes = if newline.is_some() {
                0
            } else {
                self.line_bytes + length
            };
            Ok(Some(&self.bytes[start..self.offset]))
        }
    }

    impl StopHandle {
        /// Attempts graceful shutdown while independently enforcing its deadline.
        pub(super) fn shutdown(&self, deadline: Instant) -> Result<(), String> {
            let writer = self.writer.clone();
            let stdin = self.stdin.clone();
            let _ = thread::spawn(move || {
                let _ = stdin.close();
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
            _stdin: Option<String>,
        ) -> Result<super::Boundary, super::OperationFailure> {
            unreachable!("unsupported workers cannot start")
        }

        pub(super) fn provide_input(
            &mut self,
            _stdin: String,
        ) -> Result<super::Boundary, super::OperationFailure> {
            unreachable!("unsupported workers cannot start")
        }
    }

    impl StopHandle {
        pub(super) fn shutdown(&self, _deadline: std::time::Instant) -> Result<(), String> {
            Ok(())
        }
    }
}
