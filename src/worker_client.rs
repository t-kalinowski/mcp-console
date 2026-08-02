use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
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
    result: Mutex<Option<Result<String, String>>>,
    completed: tokio::sync::Notify,
    waiting: AtomicBool,
}

struct EvaluationWaiter<'a> {
    waiting: &'a AtomicBool,
    release: bool,
}

enum EvaluationWait {
    Running,
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

    /// Starts one cell or polls the cell that is already running.
    pub(crate) async fn send(
        &self,
        r: Option<String>,
        timeout: Duration,
    ) -> Result<String, String> {
        let evaluation = match r {
            Some(r) => self.start_evaluation(r)?,
            None => match self.current_evaluation()? {
                Some(evaluation) => evaluation,
                None => return Ok("[idle]".to_string()),
            },
        };
        let result = evaluation.wait(timeout).await?;
        match result {
            EvaluationWait::Running => Ok("[running]".to_string()),
            EvaluationWait::Completed(result) => {
                self.clear_evaluation(&evaluation)?;
                result
            }
        }
    }

    fn start_evaluation(&self, r: String) -> Result<Arc<Evaluation>, String> {
        if self.shutdown_requested()? {
            return Err("worker is shutting down".to_string());
        }

        let evaluation = Arc::new(Evaluation {
            result: Mutex::new(None),
            completed: tokio::sync::Notify::new(),
            waiting: AtomicBool::new(false),
        });
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
        let evaluation_task = tokio::task::spawn_blocking(move || client.evaluate_blocking(r));
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
            *worker = Some(platform::Worker::start(
                &self.0.program,
                &self.0.arguments,
                |stop_handle| self.register_stop_handle(stop_handle),
            )?);
        }
        let result = worker
            .as_mut()
            .expect("worker should be running")
            .evaluate(r);
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
    fn complete(&self, result: Result<String, String>) {
        let Ok(mut completed) = self.result.lock() else {
            return;
        };
        *completed = Some(result);
        self.completed.notify_one();
    }

    async fn wait(&self, timeout: Duration) -> Result<EvaluationWait, String> {
        let waiter = self.claim_waiter()?;
        if let Some(result) = self.result()? {
            waiter.keep_claimed();
            return Ok(EvaluationWait::Completed(result));
        }

        let _ = tokio::time::timeout(timeout, self.completed.notified()).await;
        match self.result()? {
            Some(result) => {
                waiter.keep_claimed();
                Ok(EvaluationWait::Completed(result))
            }
            None => Ok(EvaluationWait::Running),
        }
    }

    fn claim_waiter(&self) -> Result<EvaluationWaiter<'_>, String> {
        self.waiting
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| "another send call is already waiting for this evaluation".to_string())?;
        Ok(EvaluationWaiter {
            waiting: &self.waiting,
            release: true,
        })
    }

    fn result(&self) -> Result<Option<Result<String, String>>, String> {
        self.result
            .lock()
            .map(|result| result.clone())
            .map_err(|_| "worker evaluation result lock poisoned".to_string())
    }
}

impl EvaluationWaiter<'_> {
    fn keep_claimed(mut self) {
        self.release = false;
    }
}

impl Drop for EvaluationWaiter<'_> {
    fn drop(&mut self) {
        if self.release {
            self.waiting.store(false, Ordering::Release);
        }
    }
}

#[cfg(target_os = "macos")]
mod platform {
    use std::ffi::OsString;
    use std::path::Path;
    use std::process::Stdio;
    use std::sync::{Arc, Mutex};
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

        pub(super) fn evaluate(&mut self, _r: String) -> Result<String, String> {
            unreachable!("unsupported workers cannot start")
        }
    }

    impl StopHandle {
        pub(super) fn shutdown(&self, _deadline: std::time::Instant) -> Result<(), String> {
            Ok(())
        }
    }
}
