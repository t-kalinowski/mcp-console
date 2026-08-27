use std::collections::HashMap;
use std::io::BufReader;
use std::process::Stdio;
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use super::events::{
    OperationResult, ReadyCommitOutcome, WorkerEvent, WorkerEventDispatcher, WorkerOperationState,
};
use super::output::SendFailure;
use super::{
    PreparationOutcome, PythonPreparationCommit, RPreparationCommit, WorkerProcessOutcome,
};
use crate::relay_protocol::{JsonlReader, JsonlWriter, RelayCommand, RelayEvent};

/// Lets the relay finish its bounded group cleanup, stream drain, and protocol flush
/// after the worker's own shutdown deadline before the outer fail-safe stops it.
const RELAY_RETIREMENT_GRACE: Duration = Duration::from_secs(2);

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum RelayRetirementAllowance {
    TimelyAcceptance,
    Always,
}

/// Spawns workers through the platform's runtime boundary.
pub(super) struct WorkerRuntime;

pub(super) struct Worker {
    stdin: StdinSender,
    operation: WorkerOperationState,
    interrupts: InterruptRequests,
    shutdown_started: ShutdownAcceptance,
    ready_commit: ReadyCommit,
    relay: RelayConnection,
}

/// Requests deadline-bounded shutdown while `Worker` retains the I/O task joins.
#[derive(Clone)]
pub(super) struct WorkerShutdownHandle {
    commands: RelayCommandSender,
    operation: WorkerOperationState,
    interrupts: InterruptRequests,
    shutdown_started: ShutdownAcceptance,
    ready_commit: ReadyCommit,
    child: Arc<Mutex<crate::sandbox::SandboxedChild>>,
}

struct RelayConnection {
    child: Arc<Mutex<crate::sandbox::SandboxedChild>>,
    commands: RelayCommandSender,
    tasks: Option<Box<RelayTasks>>,
}

struct RelayTasks {
    dispatcher: WorkerEventDispatcher,
    command_writer: RelayCommandThread,
    event_reader: thread::JoinHandle<()>,
}

struct RelayCommandThread {
    sender: RelayCommandSender,
    thread: thread::JoinHandle<()>,
}

#[derive(Clone)]
pub(super) struct RelayCommandSender {
    writer: mpsc::Sender<RelayWriterMessage>,
    events: mpsc::Sender<WorkerEvent>,
}

enum RelayWriterMessage {
    Command(RelayCommand),
    Shutdown {
        deadline: Instant,
        completed: mpsc::SyncSender<Result<(), String>>,
    },
    Stop,
}

#[derive(Clone, Default)]
pub(super) struct ShutdownAcceptance(Arc<Mutex<Option<ShutdownRequest>>>);

struct ShutdownRequest {
    deadline: Instant,
    observed: Option<Instant>,
}

#[derive(Clone)]
pub(super) struct StdinSender(RelayCommandSender);

#[derive(Clone)]
/// Correlates concurrent relay interrupt commands with their completion events.
pub(super) struct InterruptRequests(Arc<Mutex<InterruptRequestState>>);

struct InterruptRequestState {
    next_request_id: u64,
    pending: HashMap<u64, mpsc::SyncSender<Result<(), String>>>,
    failure: Option<String>,
}

#[derive(Clone, Default)]
struct ReadyCommit(Arc<Mutex<Option<ReadyCommitSender>>>);

type ReadyCommitSender = mpsc::Sender<ReadyCommitOutcome>;

impl WorkerRuntime {
    /// Starts a relay in the sandbox and waits for its worker's ready message.
    pub(super) fn spawn(
        &self,
        spec: super::WorkerSpec<'_>,
        output: super::OutputTape,
        on_started: impl FnOnce(WorkerShutdownHandle) -> Result<(), String>,
        on_ready: impl FnOnce() -> Result<(), String>,
    ) -> Result<Worker, SendFailure> {
        let super::WorkerSpec {
            executable,
            arguments,
            relay,
            python,
            managed_r,
            dynamic_resolution,
            callbacks,
        } = spec;

        let use_builtin_relay = relay.is_none();
        let relay_executable = match relay {
            Some(relay) => relay.to_path_buf(),
            None => std::env::current_exe().map_err(|error| {
                format!("failed to locate the worker relay executable: {error}")
            })?,
        };
        let mut command = crate::sandbox::SandboxedCommand::new(relay_executable.as_os_str())
            .map_err(|error| format!("failed to prepare worker sandbox: {error}"))?;
        if let Some(python) = python {
            python.configure_worker(&mut command);
        }
        if let Some(managed_r) = managed_r {
            managed_r.configure_worker(&mut command)?;
        }
        command.env(
            "MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION",
            if dynamic_resolution { "1" } else { "0" },
        );
        if use_builtin_relay {
            command.arg("worker-relay");
        }
        command
            .arg(executable.as_os_str())
            .args(arguments)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .new_process_group();

        let (worker_events, worker_event_receiver) = mpsc::channel();

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

        let operation = WorkerOperationState::new();
        let interrupts = InterruptRequests::new();
        let (startup_sender, startup_receiver) = mpsc::sync_channel(1);
        let (ready_commit_sender, ready_commit_receiver) = mpsc::channel();
        let ready_commit = ReadyCommit(Arc::new(Mutex::new(Some(ready_commit_sender))));
        let shutdown_started = ShutdownAcceptance::default();

        let (commands, command_writer) =
            start_relay_command_writer(relay_stdin, worker_events.clone());
        let event_reader = start_relay_event_reader(relay_stdout, worker_events);
        let dispatcher = WorkerEventDispatcher::start(
            worker_event_receiver,
            operation.clone(),
            commands.clone(),
            output.clone(),
            callbacks,
            startup_sender,
            ready_commit_receiver,
            interrupts.clone(),
            shutdown_started.clone(),
        );

        let relay = RelayConnection {
            child,
            commands: commands.clone(),
            tasks: Some(Box::new(RelayTasks {
                dispatcher,
                command_writer,
                event_reader,
            })),
        };
        let mut worker = Worker {
            stdin: StdinSender(commands.clone()),
            operation,
            interrupts,
            shutdown_started,
            ready_commit,
            relay,
        };

        if let Err(error) = on_started(worker.shutdown_handle()) {
            let error = worker.startup_failure(error);
            return Err(error);
        }
        match startup_receiver.recv() {
            Ok(Ok(())) => {}
            Ok(Err(error)) => {
                return Err(worker.startup_failure(error));
            }
            Err(_) => {
                let error = "worker event dispatcher stopped before readiness".to_string();
                return Err(worker.startup_failure(error));
            }
        }
        if let Err(error) = on_ready() {
            let error = worker.startup_failure(error);
            return Err(error);
        }
        if !worker.ready_commit.finish(ReadyCommitOutcome::Committed) {
            return Err(
                worker.startup_failure("worker stopped before readiness was committed".to_string())
            );
        }
        Ok(worker)
    }
}

impl Worker {
    pub(super) fn reserve_environment_preparation(
        &self,
    ) -> Result<
        super::events::EnvironmentPreparationReservation,
        super::EnvironmentPreparationAdmissionFailure,
    > {
        self.operation.reserve_environment_preparation()
    }

    pub(super) fn prepare_r(
        &mut self,
        library: &std::path::Path,
        commit: RPreparationCommit,
    ) -> Result<PreparationOutcome, String> {
        let library = library
            .to_str()
            .ok_or_else(|| "resolved R library path is not UTF-8".to_string())?
            .to_string();
        let result = self
            .operation
            .begin_r_preparation(library.clone(), commit)?;
        self.relay
            .commands
            .send(RelayCommand::PrepareR { library })?;
        match receive_operation(result)? {
            OperationResult::RPrepared(result) => Ok(result),
            _ => Err("worker sent an unexpected R preparation message".to_string()),
        }
    }

    pub(super) fn prepare_python(
        &mut self,
        packages: Vec<String>,
        continue_environment_preparation: bool,
        commit: PythonPreparationCommit,
    ) -> Result<PreparationOutcome, String> {
        let result = self
            .operation
            .begin_python_preparation(commit, continue_environment_preparation)?;
        self.relay
            .commands
            .send(RelayCommand::PreparePython { packages })?;
        match receive_operation(result)? {
            OperationResult::PythonPrepared(result) => Ok(result),
            _ => Err("worker sent an unexpected Python preparation message".to_string()),
        }
    }

    pub(super) fn evaluate(
        &mut self,
        cell: crate::cell::Cell,
        evaluation: Arc<super::Evaluation>,
        capture_idle_prelude: bool,
    ) -> Result<(), String> {
        let result = self
            .operation
            .begin_cell(evaluation.clone(), capture_idle_prelude)?;
        let crate::cell::Cell { language, source } = cell;
        // Attaching drains stdin bundled with this cell through the relay's sole
        // command sender. Do this before queuing Evaluate to preserve the public
        // stdin-then-evaluate transport order.
        if let Err(error) = evaluation.attach_writer(self.stdin.clone()) {
            self.operation.fail(error.clone());
            return Err(error);
        }
        if let Err(error) = self
            .relay
            .commands
            .send(RelayCommand::Evaluate { language, source })
        {
            self.operation.fail(error.clone());
            return Err(error);
        }
        match receive_operation(result)? {
            OperationResult::Completed => Ok(()),
            _ => Err("worker sent an unexpected evaluation result".to_string()),
        }
    }

    pub(super) fn idle_response_snapshot(
        &self,
        output: &super::OutputTape,
    ) -> Result<super::IdleResponseSnapshot, String> {
        self.operation.idle_response_snapshot(output)
    }

    pub(super) fn has_failure(&self) -> Result<bool, String> {
        self.operation.has_failure()
    }

    pub(super) fn write_stdin(&self, stdin: String) -> Result<(), String> {
        self.stdin.send(stdin)
    }

    pub(super) fn shutdown_after_failure(
        &mut self,
    ) -> Result<Option<WorkerProcessOutcome>, super::WorkerRetirementFailure> {
        let deadline = Instant::now();
        let retirement_deadline = relay_retirement_deadline(deadline);
        let shutdown = self.shutdown_handle();
        let (_, requested) = shutdown.request_shutdown(deadline, retirement_deadline);
        let process = combine_shutdown_results(
            requested,
            shutdown.finish_shutdown(deadline, RelayRetirementAllowance::Always),
        );
        let retirement = self.finish_retirement();
        match (process, retirement) {
            (Ok(()), Ok(outcome)) => Ok(outcome),
            (Err(error), Ok(outcome)) => Err(super::WorkerRetirementFailure::new(error, outcome)),
            (Ok(()), Err(error)) => Err(super::WorkerRetirementFailure::new(error, None)),
            (Err(error), Err(retirement_error)) => Err(super::WorkerRetirementFailure::new(
                format!("{error}; additionally failed to retire worker I/O: {retirement_error}"),
                None,
            )),
        }
    }

    pub(super) fn finish_retirement(&mut self) -> Result<Option<WorkerProcessOutcome>, String> {
        self.relay.finish_tasks()
    }

    pub(super) fn shutdown_handle(&self) -> WorkerShutdownHandle {
        WorkerShutdownHandle {
            commands: self.relay.commands(),
            operation: self.operation.clone(),
            interrupts: self.interrupts.clone(),
            shutdown_started: self.shutdown_started.clone(),
            ready_commit: self.ready_commit.clone(),
            child: self.relay.child.clone(),
        }
    }

    fn startup_failure(&mut self, message: String) -> SendFailure {
        let retirement = if self
            .ready_commit
            .finish(ReadyCommitOutcome::Failed(message.clone()))
        {
            self.shutdown_after_failure()
        } else {
            self.finish_retirement().map_err(Into::into)
        };
        match retirement {
            Ok(outcome) => SendFailure::from(message).worker_outcome(outcome),
            Err(error) => error.attach_to(SendFailure::from(message)),
        }
    }
}

fn receive_operation(
    receiver: mpsc::Receiver<Result<OperationResult, String>>,
) -> Result<OperationResult, String> {
    receiver
        .recv()
        .map_err(|_| "worker event dispatcher stopped".to_string())?
}

fn start_relay_command_writer(
    relay_stdin: std::process::ChildStdin,
    events: mpsc::Sender<WorkerEvent>,
) -> (RelayCommandSender, RelayCommandThread) {
    let (writer, receiver) = mpsc::channel();
    let sender = RelayCommandSender {
        writer,
        events: events.clone(),
    };
    let thread = thread::spawn(move || {
        let mut writer = JsonlWriter::new(relay_stdin);
        for message in receiver {
            let (command, completed) = match message {
                RelayWriterMessage::Command(command) => (command, None),
                RelayWriterMessage::Shutdown {
                    deadline,
                    completed,
                } => (
                    RelayCommand::Shutdown {
                        grace_millis: duration_millis_ceil(
                            deadline.saturating_duration_since(Instant::now()),
                        ),
                    },
                    Some(completed),
                ),
                RelayWriterMessage::Stop => return,
            };
            if let Err(error) = writer.send(&command) {
                let error = format!("worker relay stdin write failed: {error}");
                let _ = events.send(WorkerEvent::TransportFailure(error.clone()));
                if let Some(completed) = completed {
                    let _ = completed.send(Err(error));
                }
                return;
            }
            if let Some(completed) = completed {
                let _ = completed.send(Ok(()));
            }
        }
    });
    (sender.clone(), RelayCommandThread { sender, thread })
}

fn start_relay_event_reader(
    relay_stdout: std::process::ChildStdout,
    events: mpsc::Sender<WorkerEvent>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let mut reader = JsonlReader::new(BufReader::new(relay_stdout));
        let result = (|| -> Result<(), String> {
            while let Some(event) = reader
                .receive::<RelayEvent>()
                .map_err(|error| format!("worker relay stdout read failed: {error}"))?
            {
                events
                    .send(WorkerEvent::Relay(event))
                    .map_err(|_| "worker event dispatcher stopped".to_string())?;
            }
            Ok(())
        })();
        if let Err(error) = result {
            let _ = events.send(WorkerEvent::TransportFailure(error));
        }
        let _ = events.send(WorkerEvent::RelayClosed);
    })
}

impl RelayCommandSender {
    pub(super) fn send(&self, command: RelayCommand) -> Result<(), String> {
        self.writer
            .send(RelayWriterMessage::Command(command))
            .map_err(|_| self.command_writer_stopped())
    }

    fn stop(&self) {
        let _ = self.writer.send(RelayWriterMessage::Stop);
    }

    fn shutdown(
        &self,
        worker_deadline: Instant,
        completion_deadline: Instant,
    ) -> Result<(), String> {
        let (completed, wait) = mpsc::sync_channel(1);
        self.writer
            .send(RelayWriterMessage::Shutdown {
                deadline: worker_deadline,
                completed,
            })
            .map_err(|_| self.command_writer_stopped())?;
        match wait.recv_timeout(completion_deadline.saturating_duration_since(Instant::now())) {
            Ok(result) => result,
            Err(mpsc::RecvTimeoutError::Timeout) => Err(self.report_command_transport_failure(
                "worker relay shutdown command writer did not finish before the worker deadline"
                    .to_string(),
            )),
            Err(mpsc::RecvTimeoutError::Disconnected) => Err(self
                .report_command_transport_failure(
                    "worker relay shutdown command writer stopped before reporting completion"
                        .to_string(),
                )),
        }
    }

    fn retire_operation(&self, deadline: Instant, error: String) -> Result<(), String> {
        let (reached, wait) = mpsc::sync_channel(1);
        self.events
            .send(WorkerEvent::RetireOperation { error, reached })
            .map_err(|_| {
                "worker event dispatcher stopped before ordered operation retirement".to_string()
            })?;
        match wait.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
            Ok(()) => Ok(()),
            Err(mpsc::RecvTimeoutError::Timeout) => Err(
                "worker event dispatcher did not retire the operation before the relay retirement deadline"
                    .to_string(),
            ),
            Err(mpsc::RecvTimeoutError::Disconnected) => Err(
                "worker event dispatcher stopped before retiring the operation".to_string(),
            ),
        }
    }

    fn command_writer_stopped(&self) -> String {
        self.report_command_transport_failure("worker relay command writer stopped".to_string())
    }

    fn report_command_transport_failure(&self, error: String) -> String {
        let _ = self
            .events
            .send(WorkerEvent::TransportFailure(error.clone()));
        error
    }
}

impl StdinSender {
    pub(super) fn send(&self, data: String) -> Result<(), String> {
        self.0.send(RelayCommand::Stdin { data })
    }
}

impl InterruptRequests {
    fn new() -> Self {
        Self(Arc::new(Mutex::new(InterruptRequestState {
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

    pub(super) fn complete(&self, request_id: u64, error: Option<String>) -> Result<(), String> {
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

    pub(super) fn fail(&self, error: String) {
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

    pub(super) fn observe(&self) -> Result<(), String> {
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
    pub(super) fn shutdown(&self, deadline: Instant) -> Result<(), String> {
        let (allowance, requested) = self.request_shutdown(deadline, deadline);
        combine_shutdown_results(requested, self.finish_shutdown(deadline, allowance))
    }

    pub(super) fn request_shutdown(
        &self,
        worker_deadline: Instant,
        completion_deadline: Instant,
    ) -> (RelayRetirementAllowance, Result<(), String>) {
        self.ready_commit.finish(ReadyCommitOutcome::Retiring);
        let requested = self.shutdown_started.request(worker_deadline);
        let allowance = if self
            .commands
            .shutdown(worker_deadline, completion_deadline)
            .is_ok()
        {
            RelayRetirementAllowance::TimelyAcceptance
        } else {
            RelayRetirementAllowance::Always
        };
        (allowance, requested)
    }

    pub(super) fn finish_shutdown(
        &self,
        deadline: Instant,
        allowance: RelayRetirementAllowance,
    ) -> Result<(), String> {
        let retirement_deadline = relay_retirement_deadline(deadline);
        let barrier_deadline = if allowance == RelayRetirementAllowance::Always {
            retirement_deadline
        } else {
            deadline
        };
        let _ = self.retire_operation(
            barrier_deadline,
            "worker stopped before operation completed".to_string(),
        );
        self.stop_relay(deadline, retirement_deadline, allowance)
    }

    fn retire_operation(&self, deadline: Instant, error: String) -> Result<(), String> {
        self.commands.retire_operation(deadline, error)
    }

    fn stop_relay(
        &self,
        deadline: Instant,
        retirement_deadline: Instant,
        allowance: RelayRetirementAllowance,
    ) -> Result<(), String> {
        let mut child = self
            .child
            .lock()
            .map_err(|_| "worker child lock poisoned".to_string())?;
        let remaining = deadline.saturating_duration_since(Instant::now());
        let wait = match child.wait_timeout_without_reaping(remaining) {
            Ok(true) => Ok(()),
            Ok(false) => match self.should_wait_for_relay_retirement(allowance) {
                Ok(true) => child
                    .wait_timeout_without_reaping(
                        retirement_deadline.saturating_duration_since(Instant::now()),
                    )
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
    }

    fn should_wait_for_relay_retirement(
        &self,
        allowance: RelayRetirementAllowance,
    ) -> Result<bool, String> {
        if allowance == RelayRetirementAllowance::Always || self.operation.has_failure()? {
            return Ok(true);
        }
        self.shutdown_started.observed_by_deadline()
    }
}

fn relay_retirement_deadline(deadline: Instant) -> Instant {
    deadline
        .checked_add(RELAY_RETIREMENT_GRACE)
        .unwrap_or(deadline)
}

fn combine_shutdown_results(
    first: Result<(), String>,
    second: Result<(), String>,
) -> Result<(), String> {
    match (first, second) {
        (Ok(()), Ok(())) => Ok(()),
        (Err(error), Ok(())) | (Ok(()), Err(error)) => Err(error),
        (Err(error), Err(additional)) => Err(format!("{error}; additionally {additional}")),
    }
}

fn duration_millis_ceil(duration: Duration) -> u64 {
    let millis = duration.as_millis();
    let rounded = millis + u128::from(!duration.subsec_nanos().is_multiple_of(1_000_000));
    u64::try_from(rounded).unwrap_or(u64::MAX)
}

impl RelayConnection {
    fn commands(&self) -> RelayCommandSender {
        self.commands.clone()
    }

    fn finish_tasks(&mut self) -> Result<Option<WorkerProcessOutcome>, String> {
        let Some(tasks) = self.tasks.take() else {
            return Ok(None);
        };
        let command_writer =
            join_worker_thread(tasks.command_writer.stop(), "relay command writer");
        let event_reader = join_worker_thread(tasks.event_reader, "relay event reader");
        let outcome = tasks.dispatcher.join();
        command_writer.and(event_reader).and(outcome)
    }
}

impl RelayCommandThread {
    fn stop(self) -> thread::JoinHandle<()> {
        self.sender.stop();
        self.thread
    }
}

impl ReadyCommit {
    fn finish(&self, outcome: ReadyCommitOutcome) -> bool {
        let sender = match self.0.lock() {
            Ok(mut sender) => sender.take(),
            Err(poisoned) => poisoned.into_inner().take(),
        };
        if let Some(sender) = sender {
            let _ = sender.send(outcome);
            true
        } else {
            false
        }
    }
}

fn join_worker_thread(thread: thread::JoinHandle<()>, name: &str) -> Result<(), String> {
    thread
        .join()
        .map_err(|_| format!("worker {name} task failed"))
}
