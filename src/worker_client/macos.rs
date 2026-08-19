use std::io::{Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::process::Stdio;
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::Instant;

use super::TerminalCommit;
use super::activity::{Activity, IdleSynchronization, OperationResult};
use crate::worker_protocol::{ServerMessage, WorkerMessage};

/// Spawns workers through the platform's runtime boundary.
pub(super) struct WorkerRuntime;

pub(super) struct Worker {
    writer: crate::sideband::Writer,
    stdin: StdinSender,
    activity: Activity,
    sideband_cancel: WorkerCancellation,
    process: WorkerProcess,
}

/// Requests deadline-bounded shutdown while `Worker` retains the I/O task joins.
#[derive(Clone)]
pub(super) struct WorkerShutdownHandle {
    writer: crate::sideband::Writer,
    stdin: StdinSender,
    sideband_cancel: WorkerCancellation,
    child: Arc<Mutex<crate::sandbox::SandboxedChild>>,
}

struct WorkerProcess {
    child: Arc<Mutex<crate::sandbox::SandboxedChild>>,
    threads: Option<WorkerThreads>,
}

struct WorkerThreads {
    sideband: Option<WorkerIoThread>,
    sideband_guard: Option<OwnedFd>,
    stdin: WorkerIoThread,
    stdout: WorkerIoThread,
    stderr: WorkerIoThread,
}

struct WorkerIoThread {
    cancel: WorkerCancellation,
    thread: thread::JoinHandle<()>,
}

#[derive(Clone)]
struct WorkerCancellation(Arc<Mutex<Option<std::io::PipeWriter>>>);

pub(super) struct WorkerIoEvents {
    pub(super) ready: bool,
    pub(super) cancelled: bool,
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
        on_ready: impl FnOnce() -> Result<(), String>,
    ) -> Result<Worker, String> {
        let super::WorkerSpec {
            executable,
            arguments,
            managed_python,
            managed_r,
            callbacks,
        } = spec;
        let (mut reader, writer, child_fds) = crate::sideband::bind()
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
        let stdin_cancel = cancellation_pipe("stdin")?;
        let (sideband_cancelled, sideband_cancel) = cancellation_pipe("sideband")?;
        let stdout_cancel = cancellation_pipe("stdout")?;
        let stderr_cancel = cancellation_pipe("stderr")?;
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
        if let Err(error) = set_nonblocking(&stdin) {
            let error = format!("failed to configure worker stdin: {error}");
            return match child.force_stop() {
                Ok(()) => Err(error),
                Err(stop_error) => Err(format!(
                    "{error}; additionally failed to stop the worker: {stop_error}"
                )),
            };
        }
        let stdout = start_output_reader(stdout, output.direct_stdout(), stdout_cancel);
        let stderr = start_output_reader(stderr, output.direct_stderr(), stderr_cancel);
        let child = Arc::new(Mutex::new(child));
        let (stdin, stdin_thread) = start_stdin_writer(stdin, child.clone(), stdin_cancel);
        let process = WorkerProcess {
            child,
            threads: Some(WorkerThreads {
                sideband: None,
                sideband_guard: None,
                stdin: stdin_thread,
                stdout,
                stderr,
            }),
        };

        let mut worker = Worker {
            writer,
            stdin,
            activity: Activity::new(),
            sideband_cancel: sideband_cancel.clone(),
            process,
        };
        if let Err(error) = on_started(worker.shutdown_handle()) {
            return Err(worker.startup_failure(error));
        }
        let ready = match reader.receive() {
            Ok(message) => message,
            Err(error) => {
                return Err(worker.startup_failure(format!("worker sideband read failed: {error}")));
            }
        };
        let error = match ready {
            WorkerMessage::Ready => None,
            WorkerMessage::ConsoleOutput { data } | WorkerMessage::ConsoleDiagnostic { data } => {
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
        if let Err(error) = set_nonblocking(&reader) {
            return Err(
                worker.startup_failure(format!("failed to configure worker sideband: {error}"))
            );
        }
        let sideband_guard = duplicate_fd(&reader).map_err(|error| {
            worker.startup_failure(format!(
                "failed to retain worker sideband during retirement: {error}"
            ))
        })?;
        if let Err(error) = on_ready() {
            return Err(worker.startup_failure(error));
        }
        let sideband = worker.activity.start(
            reader,
            worker.writer.clone(),
            output,
            callbacks,
            sideband_cancelled,
        );
        let sideband = WorkerIoThread {
            cancel: sideband_cancel,
            thread: sideband,
        };
        worker.process.attach_sideband(sideband, sideband_guard);
        Ok(worker)
    }
}

impl Worker {
    /// Adds a resolved R library to the live worker's library search path.
    pub(super) fn prepare_r(
        &mut self,
        library: &std::path::Path,
    ) -> Result<TerminalCommit<Result<(), String>>, String> {
        let library = library
            .to_str()
            .ok_or_else(|| "resolved R library path is not UTF-8".to_string())?
            .to_string();
        let result = self.activity.begin_r_preparation()?;
        if let Err(error) = self.writer.send(&ServerMessage::PrepareR {
            library: library.clone(),
        }) {
            let error = format!("worker sideband write failed: {error}");
            self.activity.fail(error.clone());
            return Err(error);
        }
        receive_operation(result)?.try_map(|outcome| match outcome {
            OperationResult::RPrepared(prepared) if prepared == library => Ok(Ok(())),
            OperationResult::RPrepared(_) => {
                Err("worker prepared an unexpected R library".to_string())
            }
            OperationResult::RPreparationFailed(message) => Ok(Err(message)),
            _ => Err("worker sent an unexpected R preparation message".to_string()),
        })
    }

    /// Adds Python packages through the live worker's reticulate manifest.
    pub(super) fn prepare_python(
        &mut self,
        packages: Vec<String>,
    ) -> Result<TerminalCommit<Result<Option<crate::resolver::ManagedPython>, String>>, String>
    {
        let result = self.activity.begin_python_preparation()?;
        if let Err(error) = self.writer.send(&ServerMessage::PreparePython { packages }) {
            let error = format!("worker sideband write failed: {error}");
            self.activity.fail(error.clone());
            return Err(error);
        }
        receive_operation(result)?.try_map(|outcome| match outcome {
            OperationResult::PythonPrepared(candidate) => Ok(Ok(candidate)),
            OperationResult::PythonPreparationFailed(message) => Ok(Err(message)),
            _ => Err("worker sent an unexpected Python preparation message".to_string()),
        })
    }

    /// Sends one cell and waits for its terminal sideband message.
    pub(super) fn evaluate(
        &mut self,
        cell: crate::cell::Cell,
        evaluation: Arc<super::Evaluation>,
    ) -> Result<TerminalCommit<super::output::OutputCheckpoint>, String> {
        let (result, synchronization) = self.activity.begin_cell(evaluation.clone())?;
        let crate::cell::Cell { language, source } = cell;
        if let Err(error) = self
            .writer
            .send(&ServerMessage::Evaluate { language, source })
            .and_then(|()| {
                self.writer.send(&ServerMessage::Synchronize {
                    token: synchronization,
                })
            })
        {
            let error = format!("worker sideband write failed: {error}");
            self.activity.fail(error.clone());
            return Err(error);
        }
        if let Err(error) = evaluation.attach_writer(self.stdin.clone()) {
            self.activity.fail(error.clone());
            return Err(error);
        }
        receive_operation(result)?.try_map(|outcome| match outcome {
            OperationResult::Completed(checkpoint) => Ok(checkpoint),
            _ => Err("worker sent an unexpected evaluation result".to_string()),
        })
    }

    pub(super) fn write_stdin(&self, stdin: String) -> Result<(), String> {
        self.stdin.send(stdin.into_bytes())
    }

    pub(super) fn synchronize(&self) -> Result<IdleSynchronization, String> {
        self.activity.synchronize(&self.writer)
    }

    pub(super) fn shutdown(&mut self, deadline: Instant) -> Result<(), String> {
        let process = self
            .shutdown_handle()
            .shutdown(deadline)
            .and_then(|shutdown| join_worker_thread(shutdown, "shutdown sender"));
        let retirement = self.finish_retirement();
        match (process, retirement) {
            (Ok(()), Ok(())) => Ok(()),
            (Err(error), Ok(())) => Err(error),
            (Ok(()), Err(error)) => Err(error),
            (Err(error), Err(retirement_error)) => Err(format!(
                "{error}; additionally failed to retire worker I/O: {retirement_error}"
            )),
        }
    }

    pub(super) fn finish_retirement(&mut self) -> Result<(), String> {
        self.process.finish_threads()
    }

    pub(super) fn shutdown_handle(&self) -> WorkerShutdownHandle {
        WorkerShutdownHandle {
            writer: self.writer.clone(),
            stdin: self.stdin.clone(),
            sideband_cancel: self.sideband_cancel.clone(),
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

fn receive_operation(
    receiver: mpsc::Receiver<Result<TerminalCommit<OperationResult>, String>>,
) -> Result<TerminalCommit<OperationResult>, String> {
    receiver
        .recv()
        .map_err(|_| "worker sideband dispatcher stopped".to_string())?
}

fn start_output_reader(
    stream: impl Read + AsRawFd + Send + 'static,
    output: super::DirectOutput,
    (cancelled, cancel): (std::io::PipeReader, WorkerCancellation),
) -> WorkerIoThread {
    let thread = thread::spawn(move || {
        let mut stream = stream;
        let mut buffer = [0; 8 * 1024];
        while let Ok(events) = wait_for_worker_io(stream.as_raw_fd(), libc::POLLIN, &cancelled) {
            if events.cancelled {
                drain_buffered_output(&mut stream, &output, &mut buffer);
                break;
            }
            if !events.ready {
                continue;
            }
            match stream.read(&mut buffer) {
                Ok(0) => break,
                Ok(length) => output.push(&buffer[..length]),
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => break,
            }
        }
        output.close();
    });
    WorkerIoThread { cancel, thread }
}

fn start_stdin_writer(
    mut stdin: std::process::ChildStdin,
    child: Arc<Mutex<crate::sandbox::SandboxedChild>>,
    (cancelled, cancel): (std::io::PipeReader, WorkerCancellation),
) -> (StdinSender, WorkerIoThread) {
    let (sender, receiver) = mpsc::channel();
    let thread = thread::spawn(move || {
        for message in receiver {
            match message {
                StdinMessage::Write(bytes) => {
                    if write_worker_stdin(&mut stdin, &cancelled, &bytes).is_err() {
                        stop_worker_after_stdin_failure(&child);
                        return;
                    }
                }
                StdinMessage::Close => return,
            }
        }
    });
    (StdinSender(sender), WorkerIoThread { cancel, thread })
}

fn write_worker_stdin(
    stdin: &mut std::process::ChildStdin,
    cancelled: &std::io::PipeReader,
    mut bytes: &[u8],
) -> std::io::Result<()> {
    while !bytes.is_empty() {
        let events = wait_for_worker_io(stdin.as_raw_fd(), libc::POLLOUT, cancelled)?;
        if events.cancelled {
            return Ok(());
        }
        if !events.ready {
            continue;
        }
        match stdin.write(bytes) {
            Ok(0) => return Err(std::io::ErrorKind::WriteZero.into()),
            Ok(length) => bytes = &bytes[length..],
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => continue,
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

fn drain_buffered_output(
    stream: &mut (impl Read + AsRawFd),
    output: &super::DirectOutput,
    buffer: &mut [u8],
) {
    let mut remaining: libc::c_int = 0;
    // SAFETY: `stream` remains open and `remaining` points to writable storage
    // of the type expected by FIONREAD.
    if unsafe { libc::ioctl(stream.as_raw_fd(), libc::FIONREAD, &mut remaining) } < 0 {
        return;
    }
    let mut remaining = remaining.max(0) as usize;
    while remaining > 0 {
        let length = remaining.min(buffer.len());
        match stream.read(&mut buffer[..length]) {
            Ok(0) => break,
            Ok(length) => {
                output.push(&buffer[..length]);
                remaining -= length;
            }
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }
}

pub(super) fn wait_for_worker_io(
    descriptor: RawFd,
    events: libc::c_short,
    cancelled: &std::io::PipeReader,
) -> std::io::Result<WorkerIoEvents> {
    loop {
        let mut descriptors = [
            libc::pollfd {
                fd: descriptor,
                events,
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
        if unsafe { libc::poll(descriptors.as_mut_ptr(), descriptors.len() as _, -1) } >= 0 {
            return Ok(WorkerIoEvents {
                ready: descriptors[0].revents != 0,
                cancelled: descriptors[1].revents != 0,
            });
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

fn stop_worker_after_stdin_failure(child: &Arc<Mutex<crate::sandbox::SandboxedChild>>) {
    if let Ok(mut child) = child.lock() {
        let _ = child.force_stop();
    }
}

fn cancellation_pipe(stream: &str) -> Result<(std::io::PipeReader, WorkerCancellation), String> {
    let (cancelled, cancel) = std::io::pipe()
        .map_err(|error| format!("failed to create worker {stream} cancellation pipe: {error}"))?;
    Ok((
        cancelled,
        WorkerCancellation(Arc::new(Mutex::new(Some(cancel)))),
    ))
}

fn set_nonblocking(descriptor: &impl AsRawFd) -> std::io::Result<()> {
    let descriptor = descriptor.as_raw_fd();
    // SAFETY: `descriptor` is an open worker pipe owned by the caller.
    let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFL) };
    if flags < 0 {
        return Err(std::io::Error::last_os_error());
    }
    // SAFETY: this preserves the existing file status flags and adds O_NONBLOCK.
    if unsafe { libc::fcntl(descriptor, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

fn duplicate_fd(descriptor: &impl AsRawFd) -> std::io::Result<OwnedFd> {
    // SAFETY: `descriptor` is open for the duration of the call. The returned
    // descriptor is independent and immediately transferred into `OwnedFd`.
    let duplicate = unsafe { libc::fcntl(descriptor.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 0) };
    if duplicate < 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(unsafe { OwnedFd::from_raw_fd(duplicate) })
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
    pub(super) fn interrupt(&self) -> Result<(), String> {
        self.child
            .lock()
            .map_err(|_| "worker process lock poisoned".to_string())?
            .interrupt()
    }

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

        let stopped = (|| {
            let mut child = self
                .child
                .lock()
                .map_err(|_| "worker child lock poisoned".to_string())?;
            let remaining = deadline.saturating_duration_since(Instant::now());
            if child.wait_timeout(remaining)?.is_none() {
                child.force_stop()?;
            }
            Ok(())
        })();
        self.sideband_cancel.cancel();
        stopped.map(|()| shutdown)
    }
}

impl WorkerProcess {
    fn attach_sideband(&mut self, sideband: WorkerIoThread, guard: OwnedFd) {
        let threads = self
            .threads
            .as_mut()
            .expect("worker threads should still be active");
        threads.sideband = Some(sideband);
        threads.sideband_guard = Some(guard);
    }

    fn finish_threads(&mut self) -> Result<(), String> {
        let Some(threads) = self.threads.take() else {
            return Ok(());
        };
        let sideband = threads.sideband.map(WorkerIoThread::cancel);
        let stdin = threads.stdin.cancel();
        let stdout = threads.stdout.cancel();
        let stderr = threads.stderr.cancel();
        let sideband = sideband.map_or(Ok(()), |thread| {
            join_worker_thread(thread, "sideband reader")
        });
        let stdin = join_worker_thread(stdin, "stdin writer");
        let stdout = join_worker_thread(stdout, "stdout reader");
        let stderr = join_worker_thread(stderr, "stderr reader");
        sideband.and(stdin).and(stdout).and(stderr)
    }
}

impl WorkerIoThread {
    fn cancel(self) -> thread::JoinHandle<()> {
        self.cancel.cancel();
        self.thread
    }
}

impl WorkerCancellation {
    fn cancel(&self) {
        if let Ok(mut cancel) = self.0.lock() {
            drop(cancel.take());
        }
    }
}

fn join_worker_thread(thread: thread::JoinHandle<()>, name: &str) -> Result<(), String> {
    thread
        .join()
        .map_err(|_| format!("worker {name} task failed"))
}
