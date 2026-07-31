use std::path::PathBuf;
use std::sync::{Arc, Mutex};

/// A cloneable handle to one lazily started development worker.
#[derive(Clone)]
pub(crate) struct Client(Arc<ClientState>);

struct ClientState {
    path: PathBuf,
    process: Mutex<Option<platform::Worker>>,
    lifecycle: Mutex<Lifecycle>,
}

/// Couples lazy startup with shutdown so either side observes the other.
enum Lifecycle {
    Open(Option<platform::WorkerControl>),
    Shutdown,
}

impl Client {
    pub(crate) fn new(path: PathBuf) -> Self {
        Self(Arc::new(ClientState {
            path,
            process: Mutex::new(None),
            lifecycle: Mutex::new(Lifecycle::Open(None)),
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
        if self.is_shutdown()? {
            return Err("worker is shutting down".to_string());
        }

        let mut process = self
            .0
            .process
            .lock()
            .map_err(|_| "worker process lock poisoned".to_string())?;
        if self.is_shutdown()? {
            return Err("worker is shutting down".to_string());
        }

        if process.is_none() {
            *process = Some(platform::Worker::start(&self.0.path, |control| {
                self.publish(control);
            })?);
        }
        process
            .as_mut()
            .expect("worker should be running")
            .evaluate(r)
    }

    fn is_shutdown(&self) -> Result<bool, String> {
        self.0
            .lifecycle
            .lock()
            .map(|lifecycle| matches!(*lifecycle, Lifecycle::Shutdown))
            .map_err(|_| "worker lifecycle lock poisoned".to_string())
    }

    fn publish(&self, control: platform::WorkerControl) {
        let shutdown = match self.0.lifecycle.lock() {
            Ok(mut lifecycle) => match &mut *lifecycle {
                Lifecycle::Open(current) => {
                    *current = Some(control.clone());
                    false
                }
                Lifecycle::Shutdown => true,
            },
            Err(_) => true,
        };
        if shutdown {
            control.shutdown();
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
        let control = {
            let mut lifecycle = self
                .0
                .lifecycle
                .lock()
                .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
            match std::mem::replace(&mut *lifecycle, Lifecycle::Shutdown) {
                Lifecycle::Open(control) => control,
                Lifecycle::Shutdown => None,
            }
        };
        if let Some(control) = control {
            control.shutdown();
        }
        Ok(())
    }
}

#[cfg(unix)]
mod platform {
    use std::path::Path;
    use std::process::{Child, Command, Stdio};
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant};

    use base64::Engine as _;
    use base64::engine::general_purpose::STANDARD;
    use serde::{Deserialize, Serialize};

    const SHUTDOWN_GRACE: Duration = Duration::from_secs(1);
    const CHILD_POLL_INTERVAL: Duration = Duration::from_millis(25);

    #[derive(Serialize)]
    #[serde(tag = "type", rename_all = "snake_case")]
    enum ServerMessage {
        Evaluate { r: String },
        Shutdown,
    }

    #[derive(Deserialize)]
    #[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
    enum WorkerMessage {
        Ready,
        Output { data_b64: String },
        Completed,
    }

    pub(super) struct Worker {
        reader: crate::sideband::Reader,
        control: WorkerControl,
    }

    #[derive(Clone)]
    pub(super) struct WorkerControl {
        writer: crate::sideband::Writer,
        child: Arc<Mutex<Child>>,
    }

    impl Worker {
        /// Starts an executable worker and waits for its ready message.
        pub(super) fn start(
            path: &Path,
            on_started: impl FnOnce(WorkerControl),
        ) -> Result<Self, String> {
            let (reader, writer, child_fds) = crate::sideband::bind()
                .map_err(|error| format!("failed to create worker sideband: {error}"))?;
            let mut command = Command::new(path);
            command
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            child_fds.configure(&mut command);
            let child = command
                .spawn()
                .map_err(|error| format!("failed to launch worker: {error}"))?;
            drop(child_fds);

            let control = WorkerControl {
                writer,
                child: Arc::new(Mutex::new(child)),
            };
            let mut worker = Self { reader, control };
            on_started(worker.control.clone());
            if !matches!(worker.receive()?, WorkerMessage::Ready) {
                return Err("worker did not report readiness".to_string());
            }
            Ok(worker)
        }

        /// Sends one cell and collects output until the completed message.
        pub(super) fn evaluate(&mut self, r: String) -> Result<String, String> {
            self.control
                .writer
                .send(&ServerMessage::Evaluate { r })
                .map_err(|error| format!("worker sideband write failed: {error}"))?;
            let mut output = Vec::new();
            loop {
                match self.receive()? {
                    WorkerMessage::Output { data_b64 } => {
                        output.extend(
                            STANDARD
                                .decode(data_b64)
                                .map_err(|error| format!("worker sent invalid output: {error}"))?,
                        );
                    }
                    WorkerMessage::Completed => {
                        if output.is_empty() {
                            output.extend_from_slice(b"[done]");
                        }
                        return Ok(String::from_utf8_lossy(&output).into_owned());
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
            self.control.shutdown();
        }
    }

    impl WorkerControl {
        /// Requests graceful shutdown, then kills and reaps a stalled worker.
        pub(super) fn shutdown(&self) {
            if self.has_exited() {
                return;
            }
            let _ = self.writer.send(&ServerMessage::Shutdown);
            let started = Instant::now();
            while started.elapsed() < SHUTDOWN_GRACE {
                if self.has_exited() {
                    return;
                }
                thread::sleep(CHILD_POLL_INTERVAL);
            }
            if let Ok(mut child) = self.child.lock() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }

        fn has_exited(&self) -> bool {
            self.child
                .lock()
                .is_ok_and(|mut child| matches!(child.try_wait(), Ok(Some(_))))
        }
    }
}

#[cfg(not(unix))]
mod platform {
    use std::path::Path;

    pub(super) struct Worker;

    #[derive(Clone)]
    pub(super) struct WorkerControl;

    impl Worker {
        pub(super) fn start(
            _path: &Path,
            _on_started: impl FnOnce(WorkerControl),
        ) -> Result<Self, String> {
            Err("development workers are supported only on Unix".to_string())
        }

        pub(super) fn evaluate(&mut self, _r: String) -> Result<String, String> {
            unreachable!("unsupported workers cannot start")
        }
    }

    impl WorkerControl {
        pub(super) fn shutdown(&self) {}
    }
}
