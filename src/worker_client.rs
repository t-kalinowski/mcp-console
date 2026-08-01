use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, TryLockError};
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
    Stopped(String),
}

enum Operation {
    Evaluate(String),
    Input(String),
}

enum Boundary {
    Complete(String),
    Input(String),
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
        let client = Self::with_arguments(program, vec![OsString::from("worker")]);
        #[cfg(not(target_os = "macos"))]
        {
            *client
                .0
                .state
                .lock()
                .expect("new worker state lock should not be poisoned") = WorkerState::Stopped(
                "[stopped: sandboxed R sessions are not supported on this operating system]"
                    .to_string(),
            );
        }
        Ok(client)
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
    pub(crate) async fn evaluate(&self, r: String) -> Result<String, String> {
        self.run(Operation::Evaluate(r)).await
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

        let mut state = match self.0.state.try_lock() {
            Ok(state) => state,
            Err(TryLockError::WouldBlock) => {
                return Err(match operation {
                    Operation::Evaluate(_) => {
                        "cannot evaluate R code while the session is running".to_string()
                    }
                    Operation::Input(_) => {
                        "stdin is accepted only at an R input prompt".to_string()
                    }
                });
            }
            Err(TryLockError::Poisoned(_)) => {
                return Err("worker state lock poisoned".to_string());
            }
        };
        if self.shutdown_requested()? {
            return Err("worker is shutting down".to_string());
        }

        let current = std::mem::replace(&mut *state, WorkerState::Cold);
        let (mut worker, operation) = match (operation, current) {
            (Operation::Evaluate(r), WorkerState::Cold) => {
                let worker = match platform::Worker::start(
                    &self.0.program,
                    &self.0.arguments,
                    |stop_handle| self.register_stop_handle(stop_handle),
                ) {
                    Ok(worker) => worker,
                    Err(error) => return stop(&mut state, error),
                };
                (worker, Operation::Evaluate(r))
            }
            (Operation::Evaluate(r), WorkerState::Idle(worker)) => (worker, Operation::Evaluate(r)),
            (Operation::Evaluate(_), WorkerState::InputRequired(worker)) => {
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
                if stdin.contains('\0') {
                    *state = WorkerState::InputRequired(worker);
                    return Err("stdin cannot contain NUL".to_string());
                }
                (worker, Operation::Input(stdin))
            }
            (_, WorkerState::Stopped(message)) => {
                *state = WorkerState::Stopped(message.clone());
                return Err(message);
            }
        };

        let result = match operation {
            Operation::Evaluate(r) => worker.evaluate(r),
            Operation::Input(stdin) => worker.provide_input(stdin),
        };
        match result {
            Ok(Boundary::Complete(output)) => {
                *state = WorkerState::Idle(worker);
                Ok(output)
            }
            Ok(Boundary::Input(output)) => {
                *state = WorkerState::InputRequired(worker);
                Ok(output)
            }
            Err(error) => {
                drop(worker);
                stop(&mut state, error)
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

fn stop(state: &mut WorkerState, error: String) -> Result<String, String> {
    let message = format!("[stopped: {error}]");
    *state = WorkerState::Stopped(message.clone());
    Err(message)
}

#[cfg(target_os = "macos")]
mod platform {
    use std::ffi::OsString;
    use std::io;
    use std::path::Path;
    use std::process::Stdio;
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant};

    use crate::worker_protocol::{ServerMessage, WorkerMessage};

    pub(super) struct Worker {
        reader: crate::sideband::Reader,
        stop_handle: StopHandle,
    }

    #[derive(Clone)]
    pub(super) struct StopHandle {
        writer: crate::sideband::Writer,
        child: Arc<Mutex<crate::sandbox::SandboxedChild>>,
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
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .new_process_group();
            child_fds.configure(&mut command);
            let child = command
                .spawn()
                .map_err(|error| format!("failed to launch worker: {error}"))?;
            drop(child_fds);

            let stop_handle = StopHandle {
                writer,
                child: Arc::new(Mutex::new(child)),
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

        pub(super) fn evaluate(&mut self, r: String) -> Result<super::Boundary, String> {
            self.stop_handle
                .writer
                .send(&ServerMessage::Evaluate { r })
                .map_err(|error| format!("worker sideband write failed: {error}"))?;
            self.read_boundary()
        }

        pub(super) fn provide_input(&mut self, stdin: String) -> Result<super::Boundary, String> {
            self.stop_handle
                .writer
                .send(&ServerMessage::Input { stdin })
                .map_err(|error| format!("worker sideband write failed: {error}"))?;
            self.read_boundary()
        }

        fn read_boundary(&mut self) -> Result<super::Boundary, String> {
            let mut output = String::new();
            loop {
                match self.receive()? {
                    WorkerMessage::Output { data } => output.push_str(&data),
                    WorkerMessage::InputRequested { prompt } => {
                        append_text(&mut output, prompt.trim_end());
                        append_marker(&mut output, "[input]");
                        return Ok(super::Boundary::Input(output));
                    }
                    WorkerMessage::Completed => {
                        if output.is_empty() {
                            output.push_str("[done]");
                        }
                        return Ok(super::Boundary::Complete(output));
                    }
                    WorkerMessage::Fatal { message } => return Err(message),
                    WorkerMessage::Ready => {
                        return Err("worker sent an unexpected ready message".to_string());
                    }
                }
            }
        }

        fn receive(&mut self) -> Result<WorkerMessage, String> {
            self.reader
                .receive()
                .map_err(|error| worker_io_error(&self.stop_handle.child, error))
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
            let _ = thread::spawn(move || {
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

    fn append_text(output: &mut String, text: &str) {
        if !text.is_empty() {
            output.push_str(text);
        }
    }

    fn append_marker(output: &mut String, marker: &str) {
        if !output.is_empty() && !output.ends_with('\n') {
            output.push('\n');
        }
        output.push_str(marker);
    }

    fn worker_io_error(child: &Mutex<crate::sandbox::SandboxedChild>, error: io::Error) -> String {
        let started = Instant::now();
        while started.elapsed() < Duration::from_millis(250) {
            let status = match child.lock() {
                Ok(mut child) => child.try_wait(),
                Err(_) => return format!("worker status lock poisoned: {error}"),
            };
            match status {
                Ok(Some(status)) => return format!("worker exited with {status}: {error}"),
                Ok(None) => thread::sleep(Duration::from_millis(10)),
                Err(wait_error) => {
                    return format!("worker status failed: {wait_error}; {error}");
                }
            }
        }
        format!("worker sideband read failed: {error}")
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

        pub(super) fn evaluate(&mut self, _r: String) -> Result<super::Boundary, String> {
            unreachable!("unsupported workers cannot start")
        }

        pub(super) fn provide_input(&mut self, _stdin: String) -> Result<super::Boundary, String> {
            unreachable!("unsupported workers cannot start")
        }
    }

    impl StopHandle {
        pub(super) fn shutdown(&self, _deadline: std::time::Instant) -> Result<(), String> {
            Ok(())
        }
    }
}
