use std::collections::HashMap;
use std::io::BufReader;
use std::process::Stdio;
use std::sync::{Arc, Condvar, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use super::TerminalCommit;
use super::activity::{Activity, OperationResult};
use crate::relay_protocol::{
    EncodedBytes, JsonlReader, JsonlWriter, RelayCommand, RelayEvent, RelayEventPayload,
    RelayStream,
};
use crate::worker_protocol::{ServerMessage, WorkerMessage};

/// Lets the relay finish its bounded group cleanup, stream drain, and protocol flush
/// after the worker's own shutdown deadline before the outer fail-safe stops it.
const RELAY_RETIREMENT_GRACE: Duration = Duration::from_secs(2);

/// Spawns workers through the platform's runtime boundary.
pub(super) struct WorkerRuntime;

pub(super) struct Worker {
    writer: crate::sideband::Writer,
    stdin: StdinSender,
    activity: Activity,
    activity_cancel: WorkerCancellation,
    commands: RelayCommandSender,
    interrupts: InterruptClient,
    publication_gate: PublicationGate,
    shutdown_started: ShutdownAcceptance,
    process: WorkerProcess,
}

/// Requests deadline-bounded shutdown while `Worker` retains the I/O task joins.
#[derive(Clone)]
pub(super) struct WorkerShutdownHandle {
    commands: RelayCommandSender,
    activity_cancel: WorkerCancellation,
    interrupts: InterruptClient,
    publication_gate: PublicationGate,
    shutdown_started: ShutdownAcceptance,
    child: Arc<Mutex<crate::sandbox::SandboxedChild>>,
}

struct WorkerProcess {
    child: Arc<Mutex<crate::sandbox::SandboxedChild>>,
    threads: Option<Box<WorkerThreads>>,
}

struct WorkerThreads {
    activity: Option<WorkerIoThread>,
    sideband_forwarder: WorkerIoThread,
    sideband_publisher: thread::JoinHandle<()>,
    relay_commands: RelayCommandThread,
    relay_events: thread::JoinHandle<()>,
}

struct WorkerIoThread {
    cancel: WorkerCancellation,
    thread: thread::JoinHandle<()>,
}

struct RelayCommandThread {
    sender: RelayCommandSender,
    thread: thread::JoinHandle<()>,
}

#[derive(Clone)]
struct RelayCommandSender(mpsc::Sender<RelayWriterMessage>);

enum RelayWriterMessage {
    Command(RelayCommand),
    Shutdown { deadline: Instant },
    Stop,
}

enum RelaySidebandMessage {
    Message(WorkerMessage),
    Close,
}

#[derive(Clone, Default)]
struct PublicationGate(Arc<(Mutex<PublicationGateState>, Condvar)>);

#[derive(Default)]
struct PublicationGateState {
    open: bool,
    cancelled: bool,
}

#[derive(Clone, Default)]
struct ShutdownAcceptance(Arc<Mutex<Option<ShutdownRequest>>>);

struct ShutdownRequest {
    deadline: Instant,
    observed: Option<Instant>,
}

#[derive(Clone)]
struct WorkerCancellation(Arc<Mutex<Option<std::io::PipeWriter>>>);

pub(super) struct WorkerIoEvents {
    pub(super) ready: bool,
    pub(super) cancelled: bool,
}

#[derive(Clone)]
pub(super) struct StdinSender(RelayCommandSender);

#[derive(Clone)]
struct InterruptClient(Arc<Mutex<InterruptState>>);

struct InterruptState {
    next_request_id: u64,
    pending: HashMap<u64, mpsc::SyncSender<Result<(), String>>>,
    failure: Option<String>,
}

#[derive(Clone)]
struct TransportFailure {
    activity: Activity,
    startup: Arc<Mutex<Option<StartupResultSender>>>,
    interrupts: InterruptClient,
}

type StartupResultSender = mpsc::SyncSender<Result<(), String>>;

#[derive(Clone)]
struct TerminalAcknowledger {
    sequence: Arc<Mutex<Option<u64>>>,
    commands: RelayCommandSender,
}

impl WorkerRuntime {
    /// Starts a relay in the sandbox and waits for its worker's ready message.
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

        let relay_executable = std::env::current_exe()
            .map_err(|error| format!("failed to locate the worker relay executable: {error}"))?;
        let mut command = crate::sandbox::SandboxedCommand::new(relay_executable.as_os_str())
            .map_err(|error| format!("failed to prepare worker sandbox: {error}"))?;
        if let Some(managed_python) = managed_python {
            managed_python.configure_worker(&mut command);
        }
        if let Some(managed_r) = managed_r {
            managed_r.configure_worker(&mut command)?;
        }
        command
            .arg("worker-relay")
            .arg(executable.as_os_str())
            .args(arguments)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .new_process_group();

        let (activity_reader, worker_messages) = virtual_sideband("worker messages")?;
        let (worker_commands, activity_writer) = virtual_sideband("worker commands")?;
        let (activity_cancelled, activity_cancel) = cancellation_pipe("sideband")?;
        let (forwarder_cancelled, forwarder_cancel) = cancellation_pipe("sideband forwarder")?;

        let mut child = command
            .spawn()
            .map_err(|error| format!("failed to launch worker relay: {error}"))?;
        let relay_stdin = child
            .take_stdin()
            .expect("piped worker relay stdin should be available");
        let relay_stdout = child
            .take_stdout()
            .expect("piped worker relay stdout should be available");
        let child = Arc::new(Mutex::new(child));

        let activity = Activity::new();
        let interrupts = InterruptClient::new();
        let (startup_sender, startup_receiver) = mpsc::sync_channel(1);
        let startup = Arc::new(Mutex::new(Some(startup_sender)));
        let failure = TransportFailure {
            activity: activity.clone(),
            startup,
            interrupts: interrupts.clone(),
        };
        let publication_gate = PublicationGate::default();
        let shutdown_started = ShutdownAcceptance::default();

        let (commands, relay_commands) =
            start_relay_command_writer(relay_stdin, child.clone(), failure.clone());
        let sideband_forwarder = start_sideband_forwarder(
            worker_commands,
            commands.clone(),
            failure.clone(),
            (forwarder_cancelled, forwarder_cancel),
        );
        let pending_terminal = Arc::new(Mutex::new(None));
        let (sideband_publications, sideband_publisher) =
            start_sideband_publisher(worker_messages, failure.clone(), publication_gate.clone());
        let relay_events = start_relay_event_reader(
            relay_stdout,
            sideband_publications,
            output.clone(),
            failure,
            interrupts.clone(),
            pending_terminal.clone(),
            shutdown_started.clone(),
        );

        let process = WorkerProcess {
            child,
            threads: Some(Box::new(WorkerThreads {
                activity: None,
                sideband_forwarder,
                sideband_publisher,
                relay_commands,
                relay_events,
            })),
        };
        let mut worker = Worker {
            writer: activity_writer.clone(),
            stdin: StdinSender(commands.clone()),
            activity,
            activity_cancel: activity_cancel.clone(),
            commands: commands.clone(),
            interrupts,
            publication_gate: publication_gate.clone(),
            shutdown_started,
            process,
        };

        if let Err(error) = on_started(worker.shutdown_handle()) {
            return Err(worker.startup_failure(error));
        }
        match startup_receiver.recv() {
            Ok(Ok(())) => {}
            Ok(Err(error)) => return Err(worker.startup_failure(error)),
            Err(_) => {
                return Err(worker.startup_failure(
                    "worker relay event reader stopped before readiness".to_string(),
                ));
            }
        }
        if let Err(error) = on_ready() {
            return Err(worker.startup_failure(error));
        }
        worker.publication_gate.open();

        let acknowledge_terminal = TerminalAcknowledger {
            sequence: pending_terminal,
            commands,
        };
        let activity_thread = worker.activity.start(
            activity_reader,
            activity_writer,
            output,
            callbacks,
            activity_cancelled,
            move || acknowledge_terminal.acknowledge(),
        );
        worker.process.attach_activity(WorkerIoThread {
            cancel: activity_cancel,
            thread: activity_thread,
        });
        Ok(worker)
    }
}

impl Worker {
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

    pub(super) fn evaluate(
        &mut self,
        cell: crate::cell::Cell,
        evaluation: Arc<super::Evaluation>,
    ) -> Result<TerminalCommit<super::output::OutputCheckpoint>, String> {
        let result = self.activity.begin_cell(evaluation.clone())?;
        let crate::cell::Cell { language, source } = cell;
        if let Err(error) = self
            .writer
            .send(&ServerMessage::Evaluate { language, source })
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

    pub(super) fn snapshot(
        &self,
        output: &super::OutputTape,
    ) -> Result<super::WorkerSnapshot, String> {
        self.activity.snapshot(output)
    }

    pub(super) fn write_stdin(&self, stdin: String) -> Result<(), String> {
        self.stdin.send(stdin.into_bytes())
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
            commands: self.commands.clone(),
            activity_cancel: self.activity_cancel.clone(),
            interrupts: self.interrupts.clone(),
            publication_gate: self.publication_gate.clone(),
            shutdown_started: self.shutdown_started.clone(),
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

fn virtual_sideband(
    name: &str,
) -> Result<(crate::sideband::Reader, crate::sideband::Writer), String> {
    let (reader, writer) = std::io::pipe()
        .map_err(|error| format!("failed to create virtual {name} sideband: {error}"))?;
    set_nonblocking(&reader)
        .map_err(|error| format!("failed to configure virtual {name} sideband: {error}"))?;
    Ok((
        crate::sideband::Reader::new(reader),
        crate::sideband::Writer::new(writer),
    ))
}

fn start_sideband_forwarder(
    mut reader: crate::sideband::Reader,
    commands: RelayCommandSender,
    failure: TransportFailure,
    (cancelled, cancel): (std::io::PipeReader, WorkerCancellation),
) -> WorkerIoThread {
    use std::os::fd::AsRawFd as _;

    let thread = thread::spawn(move || {
        loop {
            while let Some(message) = match reader.receive_buffered::<ServerMessage>() {
                Ok(message) => message,
                Err(error) => {
                    failure.fail(format!("worker sideband forwarding failed: {error}"));
                    return;
                }
            } {
                if let Err(error) = commands.send(RelayCommand::WorkerMessage { message }) {
                    failure.fail(error);
                    return;
                }
            }

            let events = match wait_for_worker_io(reader.as_raw_fd(), libc::POLLIN, &cancelled) {
                Ok(events) => events,
                Err(error) => {
                    failure.fail(format!("worker sideband forwarding failed: {error}"));
                    return;
                }
            };
            if events.cancelled {
                return;
            }
            if !events.ready {
                continue;
            }
            match reader.read_chunk() {
                Ok(()) => {}
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::Interrupted | std::io::ErrorKind::WouldBlock
                    ) => {}
                Err(error) => {
                    failure.fail(format!("worker sideband forwarding failed: {error}"));
                    return;
                }
            }
        }
    });
    WorkerIoThread { cancel, thread }
}

fn start_relay_command_writer(
    relay_stdin: std::process::ChildStdin,
    child: Arc<Mutex<crate::sandbox::SandboxedChild>>,
    failure: TransportFailure,
) -> (RelayCommandSender, RelayCommandThread) {
    let (sender, receiver) = mpsc::channel();
    let sender = RelayCommandSender(sender);
    let thread = thread::spawn(move || {
        let mut writer = JsonlWriter::new(relay_stdin);
        for message in receiver {
            let command = match message {
                RelayWriterMessage::Command(command) => command,
                RelayWriterMessage::Shutdown { deadline } => RelayCommand::Shutdown {
                    grace_millis: duration_millis_ceil(
                        deadline.saturating_duration_since(Instant::now()),
                    ),
                },
                RelayWriterMessage::Stop => return,
            };
            if let Err(error) = writer.send(&command) {
                failure.fail(format!("worker relay stdin write failed: {error}"));
                stop_worker_after_transport_failure(&child);
                return;
            }
        }
    });
    (sender.clone(), RelayCommandThread { sender, thread })
}

fn start_sideband_publisher(
    worker_messages: crate::sideband::Writer,
    failure: TransportFailure,
    publication_gate: PublicationGate,
) -> (mpsc::Sender<RelaySidebandMessage>, thread::JoinHandle<()>) {
    let (sender, receiver) = mpsc::channel();
    let thread = thread::spawn(move || {
        for publication in receiver {
            match publication {
                RelaySidebandMessage::Message(message) => {
                    if !publication_gate.wait() {
                        return;
                    }
                    if let Err(error) = worker_messages.send(&message) {
                        failure.fail(format!(
                            "failed to publish worker sideband message: {error}"
                        ));
                        return;
                    }
                }
                RelaySidebandMessage::Close => return,
            }
        }
    });
    (sender, thread)
}

fn start_relay_event_reader(
    relay_stdout: std::process::ChildStdout,
    worker_messages: mpsc::Sender<RelaySidebandMessage>,
    output: super::OutputTape,
    failure: TransportFailure,
    interrupts: InterruptClient,
    pending_terminal: Arc<Mutex<Option<u64>>>,
    shutdown_started: ShutdownAcceptance,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let stdout = output.direct_stdout();
        let stderr = output.direct_stderr();
        let mut stdout_closed = false;
        let mut stderr_closed = false;
        let mut worker_sideband = Some(worker_messages);
        let mut worker_sideband_closed = false;
        let mut worker_exited = false;
        let mut relay_fatal = false;
        let mut semantically_failed = false;
        let mut expected_sequence = 0_u64;
        let mut reader = JsonlReader::new(BufReader::new(relay_stdout));

        let result = (|| -> Result<(), String> {
            while let Some(event) = reader
                .receive::<RelayEvent>()
                .map_err(|error| format!("worker relay stdout read failed: {error}"))?
            {
                if event.sequence != expected_sequence {
                    return Err(format!(
                        "worker relay event sequence {} arrived while expecting {expected_sequence}",
                        event.sequence
                    ));
                }
                expected_sequence = expected_sequence
                    .checked_add(1)
                    .ok_or_else(|| "worker relay event sequence overflowed".to_string())?;
                if worker_exited {
                    return if matches!(&event.payload, RelayEventPayload::WorkerExited { .. }) {
                        Err("worker relay reported worker exit twice".to_string())
                    } else {
                        Err("worker relay sent an event after worker exit".to_string())
                    };
                }
                match event.payload {
                    RelayEventPayload::WorkerMessage { message } => {
                        if worker_sideband_closed {
                            return Err(
                                "worker relay sent a message after closing the worker sideband"
                                    .to_string(),
                            );
                        }
                        if semantically_failed {
                            continue;
                        }
                        if failure.report_startup_message(&message)? {
                            continue;
                        }
                        if is_terminal_message(&message) {
                            let mut sequence = pending_terminal.lock().map_err(|_| {
                                "worker terminal sequence lock poisoned".to_string()
                            })?;
                            if sequence.replace(event.sequence).is_some() {
                                return Err(
                                    "worker relay sent a second unacknowledged terminal message"
                                        .to_string(),
                                );
                            }
                        }
                        if worker_sideband.as_ref().is_some_and(|worker_sideband| {
                            worker_sideband
                                .send(RelaySidebandMessage::Message(message))
                                .is_err()
                        }) {
                            worker_sideband = None;
                            semantically_failed = true;
                            failure.fail("worker sideband publisher stopped".to_string());
                        }
                    }
                    RelayEventPayload::Stdout { data } => {
                        if stdout_closed {
                            return Err(
                                "worker relay sent stdout after closing the stream".to_string()
                            );
                        }
                        stdout.push(&data.decode()?);
                    }
                    RelayEventPayload::Stderr { data } => {
                        if stderr_closed {
                            return Err(
                                "worker relay sent stderr after closing the stream".to_string()
                            );
                        }
                        stderr.push(&data.decode()?);
                    }
                    RelayEventPayload::StreamClosed { stream } => match stream {
                        RelayStream::Stdout if !stdout_closed => {
                            stdout.close();
                            stdout_closed = true;
                        }
                        RelayStream::Stderr if !stderr_closed => {
                            stderr.close();
                            stderr_closed = true;
                        }
                        _ => return Err("worker relay closed one output stream twice".to_string()),
                    },
                    RelayEventPayload::WorkerSidebandClosed => {
                        if worker_sideband_closed {
                            return Err("worker relay closed the worker sideband twice".to_string());
                        }
                        worker_sideband_closed = true;
                        if let Some(worker_sideband) = worker_sideband.take() {
                            let _ = worker_sideband.send(RelaySidebandMessage::Close);
                        }
                        failure.report_startup_error(
                            "worker sideband read failed: worker sideband closed".to_string(),
                        );
                    }
                    RelayEventPayload::InterruptResult { request_id, error } => {
                        if !semantically_failed {
                            interrupts.complete(request_id, error)?;
                        }
                    }
                    RelayEventPayload::ShutdownStarted => shutdown_started.observe()?,
                    RelayEventPayload::WorkerExited { status } => {
                        let _ = (status.code, status.signal);
                        worker_exited = true;
                    }
                    RelayEventPayload::Fatal { message } => {
                        if relay_fatal {
                            return Err("worker relay reported two fatal failures".to_string());
                        }
                        relay_fatal = true;
                        semantically_failed = true;
                        failure.fail(message);
                    }
                }
            }

            if worker_exited && worker_sideband_closed && stdout_closed && stderr_closed {
                Ok(())
            } else {
                Err("worker relay stdout closed before retirement completed".to_string())
            }
        })();

        if !stdout_closed {
            stdout.close();
        }
        if !stderr_closed {
            stderr.close();
        }
        if let Err(error) = result {
            failure.fail(error);
        }
        interrupts.fail("worker stopped before interrupt completed".to_string());
        if let Some(worker_sideband) = worker_sideband {
            let _ = worker_sideband.send(RelaySidebandMessage::Close);
        }
    })
}

fn is_terminal_message(message: &WorkerMessage) -> bool {
    matches!(
        message,
        WorkerMessage::Completed
            | WorkerMessage::RPrepared { .. }
            | WorkerMessage::RPreparationFailed { .. }
            | WorkerMessage::PythonPrepared
            | WorkerMessage::PythonPreparationFailed { .. }
    )
}

pub(super) fn wait_for_worker_io(
    descriptor: std::os::fd::RawFd,
    events: libc::c_short,
    cancelled: &std::io::PipeReader,
) -> std::io::Result<WorkerIoEvents> {
    use std::os::fd::AsRawFd as _;

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

fn stop_worker_after_transport_failure(child: &Arc<Mutex<crate::sandbox::SandboxedChild>>) {
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

fn set_nonblocking(descriptor: &impl std::os::fd::AsRawFd) -> std::io::Result<()> {
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

impl RelayCommandSender {
    fn send(&self, command: RelayCommand) -> Result<(), String> {
        self.0
            .send(RelayWriterMessage::Command(command))
            .map_err(|_| "worker relay command writer stopped".to_string())
    }

    fn stop(&self) {
        let _ = self.0.send(RelayWriterMessage::Stop);
    }

    fn shutdown(&self, deadline: Instant) -> Result<(), String> {
        self.0
            .send(RelayWriterMessage::Shutdown { deadline })
            .map_err(|_| "worker relay command writer stopped".to_string())
    }
}

impl StdinSender {
    pub(super) fn send(&self, bytes: Vec<u8>) -> Result<(), String> {
        self.0.send(RelayCommand::Stdin {
            data: EncodedBytes::from_bytes(&bytes),
        })
    }
}

impl InterruptClient {
    fn new() -> Self {
        Self(Arc::new(Mutex::new(InterruptState {
            next_request_id: 0,
            pending: HashMap::new(),
            failure: None,
        })))
    }

    fn request(&self, commands: &RelayCommandSender) -> Result<(), String> {
        let (result, receiver) = mpsc::sync_channel(1);
        let request_id = {
            let mut state = self
                .0
                .lock()
                .map_err(|_| "worker interrupt state lock poisoned".to_string())?;
            if let Some(error) = state.failure.as_ref() {
                return Err(error.clone());
            }
            let request_id = state.next_request_id;
            state.next_request_id = state.next_request_id.wrapping_add(1);
            state.pending.insert(request_id, result);
            request_id
        };
        if let Err(error) = commands.send(RelayCommand::Interrupt { request_id }) {
            self.remove(request_id);
            return Err(error);
        }
        receiver
            .recv()
            .map_err(|_| "worker interrupt response was not received".to_string())?
    }

    fn complete(&self, request_id: u64, error: Option<String>) -> Result<(), String> {
        let result = self
            .0
            .lock()
            .map_err(|_| "worker interrupt state lock poisoned".to_string())?
            .pending
            .remove(&request_id)
            .ok_or_else(|| "worker relay sent an unexpected interrupt result".to_string())?;
        let _ = result.send(error.map_or(Ok(()), Err));
        Ok(())
    }

    fn fail(&self, error: String) {
        let (pending, error) = match self.0.lock() {
            Ok(mut state) => {
                let error = state.failure.get_or_insert(error).clone();
                (std::mem::take(&mut state.pending), error)
            }
            Err(poisoned) => {
                let mut state = poisoned.into_inner();
                let error = state.failure.get_or_insert(error).clone();
                (std::mem::take(&mut state.pending), error)
            }
        };
        for (_, result) in pending {
            let _ = result.send(Err(error.clone()));
        }
    }

    fn remove(&self, request_id: u64) {
        let mut state = match self.0.lock() {
            Ok(state) => state,
            Err(poisoned) => poisoned.into_inner(),
        };
        state.pending.remove(&request_id);
    }
}

impl TransportFailure {
    fn fail(&self, error: String) {
        self.report_startup_error(error.clone());
        self.interrupts.fail(error.clone());
        self.activity.fail(error);
    }

    fn report_startup_message(&self, message: &WorkerMessage) -> Result<bool, String> {
        let mut startup = self
            .startup
            .lock()
            .map_err(|_| "worker startup state lock poisoned".to_string())?;
        let Some(sender) = startup.take() else {
            return Ok(false);
        };
        let result = match message {
            WorkerMessage::Ready => Ok(()),
            WorkerMessage::ConsoleOutput { data } | WorkerMessage::ConsoleDiagnostic { data } => {
                Err(format!("worker emitted output before readiness: {data}"))
            }
            WorkerMessage::Image { .. } => {
                Err("worker emitted an image before readiness".to_string())
            }
            _ => Err("worker did not report readiness".to_string()),
        };
        let _ = sender.send(result);
        Ok(true)
    }

    fn report_startup_error(&self, error: String) {
        let sender = match self.startup.lock() {
            Ok(mut startup) => startup.take(),
            Err(poisoned) => poisoned.into_inner().take(),
        };
        if let Some(sender) = sender {
            let _ = sender.send(Err(error));
        }
    }
}

impl TerminalAcknowledger {
    fn acknowledge(&self) -> Result<(), String> {
        let sequence = self
            .sequence
            .lock()
            .map_err(|_| "worker terminal sequence lock poisoned".to_string())?
            .take()
            .ok_or_else(|| "worker terminal sequence is missing".to_string())?;
        self.commands.send(RelayCommand::Acknowledge { sequence })
    }
}

impl PublicationGate {
    fn open(&self) {
        let (state, changed) = &*self.0;
        let mut state = state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.open = true;
        changed.notify_all();
    }

    fn wait(&self) -> bool {
        let (state, changed) = &*self.0;
        let mut state = state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        while !state.open && !state.cancelled {
            state = changed
                .wait(state)
                .unwrap_or_else(|poisoned| poisoned.into_inner());
        }
        state.open && !state.cancelled
    }

    fn cancel(&self) {
        let (state, changed) = &*self.0;
        let mut state = state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.cancelled = true;
        changed.notify_all();
    }
}

impl ShutdownAcceptance {
    fn request(&self, deadline: Instant) -> Result<(), String> {
        let mut request = self
            .0
            .lock()
            .map_err(|_| "worker relay shutdown state lock poisoned".to_string())?;
        if request.is_some() {
            return Err("worker relay shutdown was requested twice".to_string());
        }
        *request = Some(ShutdownRequest {
            deadline,
            observed: None,
        });
        Ok(())
    }

    fn observe(&self) -> Result<(), String> {
        let mut request = self
            .0
            .lock()
            .map_err(|_| "worker relay shutdown state lock poisoned".to_string())?;
        let request = request.as_mut().ok_or_else(|| {
            "worker relay reported shutdown start before shutdown was requested".to_string()
        })?;
        if request.observed.replace(Instant::now()).is_some() {
            return Err("worker relay reported shutdown start twice".to_string());
        }
        Ok(())
    }

    fn observed_by_deadline(&self) -> Result<bool, String> {
        let request = self
            .0
            .lock()
            .map_err(|_| "worker relay shutdown state lock poisoned".to_string())?;
        let request = request
            .as_ref()
            .ok_or_else(|| "worker relay shutdown was not requested".to_string())?;
        Ok(request
            .observed
            .is_some_and(|observed| observed <= request.deadline))
    }
}

impl WorkerShutdownHandle {
    pub(super) fn interrupt(&self) -> Result<(), String> {
        self.interrupts.request(&self.commands)
    }

    /// Requests relay-owned worker shutdown and enforces bounded relay retirement.
    ///
    /// The owning `Worker` separately joins all relay transport tasks.
    pub(super) fn shutdown(&self, deadline: Instant) -> Result<thread::JoinHandle<()>, String> {
        self.shutdown_started.request(deadline)?;
        let commands = self.commands.clone();
        let shutdown = thread::spawn(move || {
            let _ = commands.shutdown(deadline);
        });

        let stopped = {
            let mut child = self
                .child
                .lock()
                .map_err(|_| "worker child lock poisoned".to_string())?;
            let remaining = deadline.saturating_duration_since(Instant::now());
            let wait = match child.wait_timeout_without_reaping(remaining) {
                Ok(true) => Ok(()),
                Ok(false) => match self.shutdown_started.observed_by_deadline() {
                    Ok(true) => child
                        .wait_timeout_without_reaping(RELAY_RETIREMENT_GRACE)
                        .map(|_| ()),
                    Ok(false) => Ok(()),
                    Err(error) => Err(error),
                },
                Err(error) => Err(error),
            };
            let stop = child.force_stop();
            match (wait, stop) {
                (Ok(()), Ok(())) => Ok(()),
                (Err(error), Ok(())) => Err(error),
                (Ok(()), Err(error)) => Err(error),
                (Err(error), Err(stop_error)) => Err(format!(
                    "{error}; additionally failed to stop the worker relay: {stop_error}"
                )),
            }
        };
        self.activity_cancel.cancel();
        self.publication_gate.cancel();
        stopped.map(|()| shutdown)
    }
}

fn duration_millis_ceil(duration: Duration) -> u64 {
    let millis = duration.as_millis();
    let rounded = millis + u128::from(!duration.subsec_nanos().is_multiple_of(1_000_000));
    u64::try_from(rounded).unwrap_or(u64::MAX)
}

impl WorkerProcess {
    fn attach_activity(&mut self, activity: WorkerIoThread) {
        let threads = self
            .threads
            .as_mut()
            .expect("worker threads should still be active");
        threads.activity = Some(activity);
    }

    fn finish_threads(&mut self) -> Result<(), String> {
        let Some(threads) = self.threads.take() else {
            return Ok(());
        };
        let activity = threads.activity.map(WorkerIoThread::cancel);
        let sideband_forwarder = threads.sideband_forwarder.cancel();
        let relay_commands = threads.relay_commands.stop();
        let activity = activity.map_or(Ok(()), |thread| {
            join_worker_thread(thread, "sideband reader")
        });
        let sideband_forwarder = join_worker_thread(sideband_forwarder, "sideband forwarder");
        let sideband_publisher =
            join_worker_thread(threads.sideband_publisher, "sideband publisher");
        let relay_commands = join_worker_thread(relay_commands, "relay command writer");
        let relay_events = join_worker_thread(threads.relay_events, "relay event reader");
        activity
            .and(sideband_forwarder)
            .and(sideband_publisher)
            .and(relay_commands)
            .and(relay_events)
    }
}

impl RelayCommandThread {
    fn stop(self) -> thread::JoinHandle<()> {
        self.sender.stop();
        self.thread
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
        let mut cancel = match self.0.lock() {
            Ok(cancel) => cancel,
            Err(poisoned) => poisoned.into_inner(),
        };
        drop(cancel.take());
    }
}

fn join_worker_thread(thread: thread::JoinHandle<()>, name: &str) -> Result<(), String> {
    thread
        .join()
        .map_err(|_| format!("worker {name} task failed"))
}
