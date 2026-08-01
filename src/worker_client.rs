use std::path::PathBuf;
use std::sync::{Arc, Mutex};

/// A cloneable handle to one lazily started development worker.
#[derive(Clone)]
pub(crate) struct Client(Arc<ClientInner>);

struct ClientInner {
    program: PathBuf,
    worker: Mutex<Option<platform::Worker>>,
    shutdown_gate: Mutex<ShutdownGate>,
}

/// Keeps the current stop handle available until shutdown closes the gate.
enum ShutdownGate {
    Open {
        stop_handle: Option<platform::StopHandle>,
    },
    Closed,
}

impl Client {
    pub(crate) fn new(program: PathBuf) -> Self {
        Self(Arc::new(ClientInner {
            program,
            worker: Mutex::new(None),
            shutdown_gate: Mutex::new(ShutdownGate::Open { stop_handle: None }),
        }))
    }

    /// Evaluates one cell without blocking the async MCP server runtime.
    ///
    /// The worker starts on the first call and is then reused by later calls.
    /// `spawn_blocking` owns a cheap handle clone, not a copy of the process.
    pub(crate) async fn evaluate(&self, r: String) -> Result<String, String> {
        let client = self.clone();
        tokio::task::spawn_blocking(move || client.evaluate_blocking(r))
            .await
            .map_err(|error| format!("worker task failed: {error}"))?
    }

    fn evaluate_blocking(&self, r: String) -> Result<String, String> {
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
            *worker = Some(platform::Worker::start(&self.0.program, |stop_handle| {
                self.register_stop_handle(stop_handle)
            })?);
        }
        worker
            .as_mut()
            .expect("worker should be running")
            .evaluate(r)
    }

    fn shutdown_requested(&self) -> Result<bool, String> {
        self.0
            .shutdown_gate
            .lock()
            .map(|gate| matches!(*gate, ShutdownGate::Closed))
            .map_err(|_| "worker shutdown gate lock poisoned".to_string())
    }

    fn register_stop_handle(&self, handle: platform::StopHandle) -> Result<(), String> {
        let mut gate = self
            .0
            .shutdown_gate
            .lock()
            .map_err(|_| "worker shutdown gate lock poisoned".to_string())?;
        match &mut *gate {
            ShutdownGate::Open { stop_handle } => {
                *stop_handle = Some(handle);
                Ok(())
            }
            ShutdownGate::Closed => Err("worker is shutting down".to_string()),
        }
    }

    /// Stops and reaps the worker, including one blocked in an evaluation.
    pub(crate) async fn shutdown(&self) -> Result<(), String> {
        let client = self.clone();
        tokio::task::spawn_blocking(move || client.shutdown_blocking())
            .await
            .map_err(|error| format!("worker shutdown task failed: {error}"))?
    }

    fn shutdown_blocking(&self) -> Result<(), String> {
        let stop_handle = {
            let mut gate = self
                .0
                .shutdown_gate
                .lock()
                .map_err(|_| "worker shutdown gate lock poisoned".to_string())?;
            match std::mem::replace(&mut *gate, ShutdownGate::Closed) {
                ShutdownGate::Open { stop_handle } => stop_handle,
                ShutdownGate::Closed => None,
            }
        };
        if let Some(stop_handle) = stop_handle {
            stop_handle.shutdown();
        }
        Ok(())
    }
}

#[cfg(unix)]
mod platform {
    use std::os::unix::process::CommandExt as _;
    use std::path::Path;
    use std::process::{Child, Command, Stdio};
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    use serde::{Deserialize, Serialize};
    use wait_timeout::ChildExt as _;

    const SHUTDOWN_GRACE: Duration = Duration::from_secs(1);

    #[derive(Serialize)]
    #[serde(tag = "kind", rename_all = "snake_case")]
    enum ServerMessage {
        Evaluate { r: String },
        Shutdown,
    }

    #[derive(Deserialize)]
    #[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
    enum WorkerMessage {
        Ready,
        Output { data: String },
        Completed,
    }

    pub(super) struct Worker {
        reader: crate::sideband::Reader,
        stop_handle: StopHandle,
    }

    #[derive(Clone)]
    pub(super) struct StopHandle {
        writer: crate::sideband::Writer,
        child: Arc<Mutex<Child>>,
    }

    impl Worker {
        /// Starts an executable worker and waits for its ready message.
        pub(super) fn start(
            program: &Path,
            on_started: impl FnOnce(StopHandle) -> Result<(), String>,
        ) -> Result<Self, String> {
            let (reader, writer, child_fds) = crate::sideband::bind()
                .map_err(|error| format!("failed to create worker sideband: {error}"))?;
            let mut command = Command::new(program);
            command
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .process_group(0);
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

        /// Sends one cell and collects output until the completed message.
        pub(super) fn evaluate(&mut self, r: String) -> Result<String, String> {
            self.stop_handle
                .writer
                .send(&ServerMessage::Evaluate { r })
                .map_err(|error| format!("worker sideband write failed: {error}"))?;
            let mut output = String::new();
            loop {
                match self.receive()? {
                    WorkerMessage::Output { data } => output.push_str(&data),
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

    impl Drop for Worker {
        fn drop(&mut self) {
            self.stop_handle.shutdown();
        }
    }

    impl StopHandle {
        /// Waits for graceful shutdown, then kills and reaps a stalled worker.
        pub(super) fn shutdown(&self) {
            let _ = self.writer.send(&ServerMessage::Shutdown);
            if let Ok(mut child) = self.child.lock()
                && child.wait_timeout(SHUTDOWN_GRACE).ok().flatten().is_none()
            {
                // SAFETY: `process_group(0)` made the child's PID its process-group ID.
                let _ = unsafe { libc::killpg(child.id() as libc::pid_t, libc::SIGKILL) };
                let _ = child.wait();
            }
        }
    }
}

#[cfg(not(unix))]
mod platform {
    use std::path::Path;

    pub(super) struct Worker;

    #[derive(Clone)]
    pub(super) struct StopHandle;

    impl Worker {
        pub(super) fn start(
            _program: &Path,
            _on_started: impl FnOnce(StopHandle) -> Result<(), String>,
        ) -> Result<Self, String> {
            Err("development workers are supported only on Unix".to_string())
        }

        pub(super) fn evaluate(&mut self, _r: String) -> Result<String, String> {
            unreachable!("unsupported workers cannot start")
        }
    }

    impl StopHandle {
        pub(super) fn shutdown(&self) {}
    }
}
