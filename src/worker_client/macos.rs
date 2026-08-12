use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::process::Stdio;
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::Instant;

use crate::worker_protocol::{ServerMessage, WorkerMessage};

/// Spawns workers through the platform's runtime boundary.
pub(super) struct WorkerRuntime;

pub(super) struct Worker {
    reader: crate::sideband::Reader,
    writer: crate::sideband::Writer,
    stdin: StdinSender,
    process: WorkerProcess,
}

/// Requests deadline-bounded shutdown while `Worker` retains the I/O task joins.
#[derive(Clone)]
pub(super) struct WorkerShutdownHandle {
    writer: crate::sideband::Writer,
    stdin: StdinSender,
    child: Arc<Mutex<crate::sandbox::SandboxedChild>>,
}

struct WorkerProcess {
    child: Arc<Mutex<crate::sandbox::SandboxedChild>>,
    threads: Option<WorkerThreads>,
}

struct WorkerThreads {
    stdin: thread::JoinHandle<()>,
    stdout: OutputReader,
    stderr: OutputReader,
}

struct OutputReader {
    cancel: std::io::PipeWriter,
    thread: thread::JoinHandle<()>,
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
        output: super::OutputTape,
        on_started: impl FnOnce(WorkerShutdownHandle) -> Result<(), String>,
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
        let stdout = start_output_reader(stdout, output.stream());
        let stderr = start_output_reader(stderr, output.stream());
        let child = Arc::new(Mutex::new(child));
        let (stdin, stdin_thread) = start_stdin_writer(stdin, child.clone());
        let process = WorkerProcess {
            child,
            threads: Some(WorkerThreads {
                stdin: stdin_thread,
                stdout,
                stderr,
            }),
        };

        let mut worker = Worker {
            reader,
            writer,
            stdin,
            process,
        };
        if let Err(error) = on_started(worker.shutdown_handle()) {
            return Err(worker.startup_failure(error));
        }
        let ready = match worker.receive() {
            Ok(message) => message,
            Err(error) => return Err(worker.startup_failure(error)),
        };
        let error = match ready {
            WorkerMessage::Ready => None,
            WorkerMessage::Output { data } => {
                Some(format!("worker emitted output before readiness: {data}"))
            }
            WorkerMessage::Image { .. } => {
                Some("worker emitted an image before readiness".to_string())
            }
            _ => Some("worker did not report readiness".to_string()),
        };
        if let Some(error) = error {
            return Err(worker.startup_failure(error));
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
        self.writer
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
        self.writer
            .send(&ServerMessage::Evaluate { language, source })
            .map_err(|error| format!("worker sideband write failed: {error}"))?;
        evaluation.attach_writer(self.stdin.clone())?;
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
                self.writer
                    .send(&ServerMessage::PythonResolved { python })
                    .map_err(|error| format!("worker sideband write failed: {error}"))?;
                Ok(Some(managed))
            }
            Err(message) => {
                self.writer
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
        self.stdin.send(stdin.into_bytes())
    }

    pub(super) fn shutdown(&mut self, deadline: Instant) -> Result<(), String> {
        let shutdown = self.shutdown_handle().shutdown(deadline)?;
        join_worker_thread(shutdown, "shutdown sender")?;
        self.finish_retirement()
    }

    pub(super) fn finish_retirement(&mut self) -> Result<(), String> {
        self.process.finish_threads()
    }

    pub(super) fn shutdown_handle(&self) -> WorkerShutdownHandle {
        WorkerShutdownHandle {
            writer: self.writer.clone(),
            stdin: self.stdin.clone(),
            child: self.process.child.clone(),
        }
    }

    fn startup_failure(&mut self, message: String) -> String {
        match self.shutdown(Instant::now()) {
            Ok(()) => message,
            Err(error) => format!("{message}; additionally failed to stop the worker: {error}"),
        }
    }
}

fn start_output_reader(
    stream: impl Read + AsRawFd + Send + 'static,
    output: super::OutputTapeStream,
) -> OutputReader {
    let (cancelled, cancel) = std::io::pipe().expect("output cancellation pipe should open");
    let thread = thread::spawn(move || {
        let mut stream = stream;
        let mut buffer = [0; 8 * 1024];
        loop {
            let mut descriptors = [
                libc::pollfd {
                    fd: stream.as_raw_fd(),
                    events: libc::POLLIN,
                    revents: 0,
                },
                libc::pollfd {
                    fd: cancelled.as_raw_fd(),
                    events: libc::POLLIN,
                    revents: 0,
                },
            ];
            // SAFETY: both descriptors remain open for the call, and the pointer and
            // count describe the initialized array exactly.
            let ready = unsafe { libc::poll(descriptors.as_mut_ptr(), descriptors.len() as _, -1) };
            if ready < 0 {
                if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
                    continue;
                }
                break;
            }
            if descriptors[1].revents != 0 {
                break;
            }
            if descriptors[0].revents != 0 {
                match stream.read(&mut buffer) {
                    Ok(0) => break,
                    Ok(length) => output.push(&buffer[..length]),
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(_) => break,
                }
            }
        }
        output.close();
    });
    OutputReader { cancel, thread }
}

fn start_stdin_writer(
    mut stdin: std::process::ChildStdin,
    child: Arc<Mutex<crate::sandbox::SandboxedChild>>,
) -> (StdinSender, thread::JoinHandle<()>) {
    let (sender, receiver) = mpsc::channel();
    let thread = thread::spawn(move || {
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
    (StdinSender(sender), thread)
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

impl WorkerShutdownHandle {
    /// Closes worker input, requests protocol shutdown, and enforces the process deadline.
    ///
    /// The owning `Worker` separately joins the stdin and standard-stream tasks.
    pub(super) fn shutdown(&self, deadline: Instant) -> Result<thread::JoinHandle<()>, String> {
        let writer = self.writer.clone();
        let stdin = self.stdin.clone();
        let shutdown = thread::spawn(move || {
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
        Ok(shutdown)
    }
}

impl WorkerProcess {
    fn finish_threads(&mut self) -> Result<(), String> {
        let Some(threads) = self.threads.take() else {
            return Ok(());
        };
        let stdout = threads.stdout.cancel();
        let stderr = threads.stderr.cancel();
        let stdin = join_worker_thread(threads.stdin, "stdin writer");
        let stdout = join_worker_thread(stdout, "stdout reader");
        let stderr = join_worker_thread(stderr, "stderr reader");
        stdin.and(stdout).and(stderr)
    }
}

impl OutputReader {
    fn cancel(self) -> thread::JoinHandle<()> {
        drop(self.cancel);
        self.thread
    }
}

fn join_worker_thread(thread: thread::JoinHandle<()>, name: &str) -> Result<(), String> {
    thread
        .join()
        .map_err(|_| format!("worker {name} task failed"))
}
