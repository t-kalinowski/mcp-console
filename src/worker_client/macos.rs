use std::io::{Read, Write};
use std::process::Stdio;
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::Instant;

use crate::worker_protocol::{ServerMessage, WorkerMessage};

/// Spawns workers through the platform's runtime boundary.
pub(super) struct WorkerRuntime;

pub(super) struct Worker {
    reader: crate::sideband::Reader,
    handle: WorkerHandle,
}

/// Controls the lifecycle of one spawned worker.
#[derive(Clone)]
pub(super) struct WorkerHandle {
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

impl WorkerRuntime {
    /// Starts an executable worker and waits for its ready message.
    pub(super) fn spawn(
        &self,
        spec: super::WorkerSpec<'_>,
        output: super::CapturedOutput,
        on_started: impl FnOnce(WorkerHandle) -> Result<(), String>,
    ) -> Result<Worker, String> {
        let super::WorkerSpec {
            executable,
            arguments,
            managed_python,
            managed_r,
        } = spec;
        let (reader, writer, child_fds) = crate::sideband::bind()
            .map_err(|error| format!("failed to create worker sideband: {error}"))?;
        let mut command = crate::sandbox::SandboxedCommand::new(executable.as_os_str())
            .map_err(|error| format!("failed to prepare worker sandbox: {error}"))?;
        if let Some(managed_python) = managed_python {
            managed_python.configure_worker(&mut command);
        }
        if let Some(managed_r) = managed_r {
            managed_r.configure_worker(&mut command)?;
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

        let handle = WorkerHandle {
            writer,
            stdin,
            child,
        };
        let mut worker = Worker { reader, handle };
        on_started(worker.handle.clone())?;
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
}

impl Worker {
    /// Adds Python packages through the live worker's reticulate manifest.
    pub(super) fn prepare_python(
        &mut self,
        packages: Vec<String>,
        mut resolve_python: impl FnMut(
            crate::worker_protocol::PythonResolveRequest,
        ) -> Result<crate::resolver::ManagedPython, String>,
        mut checkpoint_python: impl FnMut(
            crate::worker_protocol::PythonRequirementManifest,
            Vec<crate::resolver::ManagedPython>,
        ) -> Result<(), String>,
    ) -> Result<Result<(), String>, String> {
        self.handle
            .writer
            .send(&ServerMessage::PreparePython { packages })
            .map_err(|error| format!("worker sideband write failed: {error}"))?;
        let mut python_candidates = Vec::new();

        loop {
            match self.receive()? {
                WorkerMessage::ResolvePython { request } => {
                    python_candidates
                        .extend(self.resolve_python_request(request, &mut resolve_python)?);
                }
                WorkerMessage::PythonPrepared { python_checkpoint } => {
                    checkpoint_python(python_checkpoint, python_candidates)?;
                    return Ok(Ok(()));
                }
                WorkerMessage::PythonPreparationFailed { message } => {
                    return Ok(Err(message));
                }
                _ => {
                    return Err("worker sent an unexpected Python preparation message".to_string());
                }
            }
        }
    }

    /// Sends one cell and collects output until the completed message.
    pub(super) fn evaluate(
        &mut self,
        cell: crate::cell::Cell,
        evaluation: &super::Evaluation,
        mut resolve_python: impl FnMut(
            crate::worker_protocol::PythonResolveRequest,
        ) -> Result<crate::resolver::ManagedPython, String>,
        mut checkpoint_python: impl FnMut(
            Option<crate::worker_protocol::PythonRequirementManifest>,
            Vec<crate::resolver::ManagedPython>,
        ) -> Result<(), String>,
    ) -> Result<(), String> {
        let crate::cell::Cell { language, source } = cell;
        self.handle
            .writer
            .send(&ServerMessage::Evaluate { language, source })
            .map_err(|error| format!("worker sideband write failed: {error}"))?;
        evaluation.attach_writer(self.handle.stdin.clone())?;
        let mut python_candidates = Vec::new();

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
                WorkerMessage::ResolvePython { request } => {
                    python_candidates
                        .extend(self.resolve_python_request(request, &mut resolve_python)?);
                }
                WorkerMessage::Completed { python_checkpoint } => {
                    evaluation.input_complete()?;
                    checkpoint_python(python_checkpoint, python_candidates)?;
                    return Ok(());
                }
                WorkerMessage::Ready => {
                    return Err("worker sent an unexpected ready message".to_string());
                }
                WorkerMessage::PythonPrepared { .. }
                | WorkerMessage::PythonPreparationFailed { .. } => {
                    return Err("worker sent an unexpected Python preparation result".to_string());
                }
            }
        }
    }

    fn resolve_python_request(
        &mut self,
        request: crate::worker_protocol::PythonResolveRequest,
        resolve_python: &mut impl FnMut(
            crate::worker_protocol::PythonResolveRequest,
        ) -> Result<crate::resolver::ManagedPython, String>,
    ) -> Result<Option<crate::resolver::ManagedPython>, String> {
        match resolve_python(request) {
            Ok(managed) => {
                let python = managed.python().to_string_lossy().into_owned();
                self.handle
                    .writer
                    .send(&ServerMessage::PythonResolved { python })
                    .map_err(|error| format!("worker sideband write failed: {error}"))?;
                Ok(Some(managed))
            }
            Err(message) => {
                self.handle
                    .writer
                    .send(&ServerMessage::PythonResolutionFailed { message })
                    .map_err(|error| format!("worker sideband write failed: {error}"))?;
                Ok(None)
            }
        }
    }

    fn receive(&mut self) -> Result<WorkerMessage, String> {
        self.reader
            .receive()
            .map_err(|error| format!("worker sideband read failed: {error}"))
    }

    pub(super) fn write_stdin(&self, stdin: String) -> Result<(), String> {
        self.handle.stdin.send(stdin.into_bytes())
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
        let _ = self.handle.force_stop();
    }
}

impl WorkerHandle {
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
