use std::collections::HashMap;
use std::ffi::OsString;
use std::io::BufReader;
use std::os::fd::{AsFd as _, AsRawFd as _, OwnedFd};
use std::os::unix::process::ExitStatusExt as _;
use std::process::{Child, ChildStdin, ChildStdout, Command, ExitStatus, Stdio};
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

/// Lets the relay finish direct-worker shutdown, stream draining, and protocol
/// flushing after the worker's deadline before the outer fail-safe stops it.
const RELAY_RETIREMENT_GRACE: Duration = Duration::from_secs(2);
/// Lets the owned launcher complete its bounded child cleanup after SIGTERM.
const LAUNCHER_RETIREMENT_GRACE: Duration = Duration::from_secs(6);
const LAUNCHER_KILL_GRACE: Duration = Duration::from_secs(1);
const CHILD_EXIT_FALLBACK_POLL_INTERVAL: Duration = Duration::from_millis(10);

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
    child: Arc<Mutex<RelayProcess>>,
}

struct RelayConnection {
    child: Arc<Mutex<RelayProcess>>,
    commands: RelayCommandSender,
    tasks: Option<Box<RelayTasks>>,
}

struct RelayProcess {
    child: Child,
    exit: super::child_exit::ChildExitWaiter,
    exited: bool,
    reaped: bool,
    ready_committed: bool,
    owned_retirement_requested: bool,
    failure_recovery_expected: bool,
    retirement: Option<Result<(), String>>,
}

struct RelayTasks {
    dispatcher: WorkerEventDispatcher,
    command_writer: RelayCommandThread,
    event_reader: thread::JoinHandle<()>,
    relay_stdout_observer: OwnedFd,
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

        let current_executable = std::env::current_exe()
            .map_err(|error| format!("failed to locate the sandbox launcher: {error}"))?;
        let target = relay_command_line(&current_executable, executable, arguments, relay);
        let mut command = Command::new(&current_executable);
        command
            .arg("sandbox")
            .arg("--exit-with-parent")
            .arg(std::process::id().to_string())
            .arg("--")
            .args(target);
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
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        crate::process_descriptors::close_unlisted_from_multithreaded_parent(&mut command)?;

        let (worker_events, worker_event_receiver) = mpsc::channel();

        let child = command
            .spawn()
            .map_err(|error| format!("failed to launch worker relay: {error}"))?;
        let mut child = RelayProcess::new(child)
            .map_err(|error| format!("failed to monitor worker relay: {error}"))?;
        let relay_stdin = child
            .take_stdin()
            .expect("piped worker relay stdin should be available");
        let relay_stdout = child
            .take_stdout()
            .expect("piped worker relay stdout should be available");
        let relay_stdout_observer = relay_stdout
            .as_fd()
            .try_clone_to_owned()
            .map_err(|error| format!("failed to monitor worker relay stdout: {error}"))?;
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
                relay_stdout_observer,
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
        if let Err(error) = worker.relay.mark_ready() {
            return Err(worker.startup_failure(error));
        }
        Ok(worker)
    }
}

fn relay_command_line(
    current_executable: &std::path::Path,
    worker_executable: &std::path::Path,
    worker_arguments: &[OsString],
    relay: Option<&std::path::Path>,
) -> Vec<OsString> {
    let mut target = match relay {
        Some(relay) => vec![relay.as_os_str().to_os_string()],
        None => vec![
            current_executable.as_os_str().to_os_string(),
            OsString::from("worker-relay"),
        ],
    };
    target.push(worker_executable.as_os_str().to_os_string());
    target.extend(worker_arguments.iter().cloned());
    target
}

impl RelayProcess {
    fn new(child: Child) -> Result<Self, String> {
        let exit = match super::child_exit::ChildExitWaiter::start(child.id()) {
            Ok(exit) => exit,
            Err(error) => {
                return Err(retire_after_exit_observer_failure(child, error));
            }
        };
        Ok(Self {
            child,
            exit,
            exited: false,
            reaped: false,
            ready_committed: false,
            owned_retirement_requested: false,
            failure_recovery_expected: false,
            retirement: None,
        })
    }

    fn take_stdin(&mut self) -> Option<ChildStdin> {
        self.child.stdin.take()
    }

    fn take_stdout(&mut self) -> Option<ChildStdout> {
        self.child.stdout.take()
    }

    fn wait_timeout_without_reaping(&mut self, timeout: Duration) -> Result<bool, String> {
        if self.has_exited()? {
            return Ok(true);
        }
        self.exited = self.exit.wait(timeout)?;
        Ok(self.exited)
    }

    fn request_retirement(&mut self) -> Result<(), String> {
        let mut errors = Vec::new();
        match self.has_exited() {
            Ok(true) => return Ok(()),
            Ok(false) => {}
            Err(error) => errors.push(error),
        }
        // SAFETY: the direct child remains unreaped here, so its PID cannot be
        // reused before `kill` returns.
        if unsafe { libc::kill(self.child.id() as libc::pid_t, libc::SIGTERM) } == 0 {
            self.owned_retirement_requested = true;
        } else {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() != Some(libc::ESRCH) {
                errors.push(format!(
                    "failed to request worker launcher retirement: {error}"
                ));
            }
        }
        collected_errors(errors)
    }

    fn force_stop(&mut self) -> Result<(), String> {
        if self.reaped {
            return self.retirement.clone().unwrap_or(Ok(()));
        }
        let cleanup = self.force_stop_inner();
        let prior = self.retirement.take();
        let result = match (prior, cleanup) {
            (None | Some(Ok(())), cleanup) => cleanup,
            (Some(Err(error)), Ok(())) => Err(error),
            (Some(Err(error)), Err(cleanup_error)) => {
                Err(format!("{error}; additionally {cleanup_error}"))
            }
        };
        self.finish_retirement(result)
    }

    fn force_stop_inner(&mut self) -> Result<(), String> {
        let mut errors = Vec::new();
        match self.has_exited() {
            Ok(true) => return self.reap(),
            Ok(false) => {}
            Err(error) => errors.push(error),
        }
        if let Err(error) = self.child.kill()
            && error.raw_os_error() != Some(libc::ESRCH)
        {
            errors.push(format!("failed to stop the worker launcher: {error}"));
            return collected_errors(errors);
        }
        let kill_deadline = Instant::now()
            .checked_add(LAUNCHER_KILL_GRACE)
            .unwrap_or_else(Instant::now);
        let mut recovered_status = None;
        let exited = match self.exit.wait(LAUNCHER_KILL_GRACE) {
            Ok(true) => {
                self.exited = true;
                true
            }
            Ok(false) => false,
            Err(error) => {
                errors.push(error);
                recovered_status = observe_and_reap_child(
                    &mut self.child,
                    kill_deadline.saturating_duration_since(Instant::now()),
                    &mut errors,
                );
                recovered_status.is_some()
            }
        };
        if let Some(status) = recovered_status {
            if let Err(error) = self.finish_reaped_status(status) {
                errors.push(error);
            }
        } else if exited {
            if let Err(error) = self.reap() {
                errors.push(error);
            }
        } else {
            errors.push(format!(
                "worker launcher remained live for {} ms after forced termination",
                LAUNCHER_KILL_GRACE.as_millis(),
            ));
            match self.child.try_wait() {
                Ok(Some(status)) => {
                    if let Err(error) = self.finish_reaped_status(status) {
                        errors.push(error);
                    }
                }
                Ok(None) => {}
                Err(error) => errors.push(format!(
                    "failed to inspect the worker launcher after forced termination: {error}"
                )),
            }
        }
        collected_errors(errors)
    }

    fn finish_retirement(&mut self, result: Result<(), String>) -> Result<(), String> {
        self.retirement = Some(result.clone());
        result
    }

    fn reap(&mut self) -> Result<(), String> {
        if self.reaped {
            return Ok(());
        }
        let status = self
            .child
            .wait()
            .map_err(|error| format!("failed to reap the worker launcher: {error}"))?;
        self.finish_reaped_status(status)
    }

    fn finish_reaped_status(&mut self, status: ExitStatus) -> Result<(), String> {
        self.exited = true;
        self.reaped = true;
        if !self.ready_committed
            || status.success()
            || (self.owned_retirement_requested || self.failure_recovery_expected)
                && status.code() == Some(128 + libc::SIGKILL)
        {
            Ok(())
        } else if let Some(code) = status.code() {
            Err(format!("worker launcher exited with status {code}"))
        } else if let Some(signal) = status.signal() {
            Err(format!("worker launcher terminated by signal {signal}"))
        } else {
            Err("worker launcher exited without a status code or signal".to_string())
        }
    }

    fn has_exited(&mut self) -> Result<bool, String> {
        if self.exited || self.reaped {
            return Ok(true);
        }
        self.exited = self.exit.wait(Duration::ZERO)?;
        Ok(self.exited)
    }

    fn is_reaped(&self) -> bool {
        self.reaped
    }
}

impl Drop for RelayProcess {
    fn drop(&mut self) {
        if self.reaped {
            return;
        }
        let _ = self.request_retirement();
        if !self
            .wait_timeout_without_reaping(LAUNCHER_RETIREMENT_GRACE)
            .unwrap_or(false)
        {
            let _ = self.force_stop_inner();
        } else {
            let _ = self.reap();
        }
    }
}

fn retire_after_exit_observer_failure(mut child: Child, error: String) -> String {
    let mut errors = vec![error];
    // SAFETY: the direct child remains unreaped, so its PID cannot be reused
    // before the signal is delivered.
    if unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM) } != 0 {
        let signal_error = std::io::Error::last_os_error();
        if signal_error.raw_os_error() != Some(libc::ESRCH) {
            errors.push(format!(
                "failed to request worker launcher retirement: {signal_error}"
            ));
        }
    }

    let mut reaped =
        observe_and_reap_child(&mut child, LAUNCHER_RETIREMENT_GRACE, &mut errors).is_some();
    if !reaped {
        if let Err(kill_error) = child.kill()
            && kill_error.raw_os_error() != Some(libc::ESRCH)
        {
            errors.push(format!("failed to stop the worker launcher: {kill_error}"));
        }
        reaped = observe_and_reap_child(&mut child, LAUNCHER_KILL_GRACE, &mut errors).is_some();
    }
    if !reaped {
        errors.push(format!(
            "worker launcher remained live for {} ms after forced termination",
            LAUNCHER_KILL_GRACE.as_millis(),
        ));
    }
    errors.join("; additionally ")
}

fn observe_and_reap_child(
    child: &mut Child,
    timeout: Duration,
    errors: &mut Vec<String>,
) -> Option<ExitStatus> {
    let deadline = Instant::now()
        .checked_add(timeout)
        .unwrap_or_else(Instant::now);
    match super::child_exit::ChildExitWaiter::start(child.id()) {
        Ok(mut exit) => match exit.wait(timeout) {
            Ok(true) => {
                return match child.wait() {
                    Ok(status) => Some(status),
                    Err(error) => {
                        errors.push(format!("failed to reap the worker launcher: {error}"));
                        None
                    }
                };
            }
            Ok(false) => {}
            Err(error) => errors.push(error),
        },
        Err(error) => errors.push(error),
    }

    // Thread creation or exit observation failed. Poll only in this exceptional
    // path so the launcher still receives a bounded grace period and is reaped.
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Some(status),
            Ok(None) => {}
            Err(error) => {
                errors.push(format!(
                    "failed to inspect the worker launcher before releasing it: {error}"
                ));
                return None;
            }
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return None;
        }
        thread::sleep(remaining.min(CHILD_EXIT_FALLBACK_POLL_INTERVAL));
    }
}

fn collected_errors(errors: Vec<String>) -> Result<(), String> {
    match errors.split_first() {
        None => Ok(()),
        Some((first, rest)) => Err(rest.iter().fold(first.clone(), |mut error, additional| {
            error.push_str("; additionally ");
            error.push_str(additional);
            error
        })),
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
        if let Some(result) = child.retirement.as_ref() {
            return result.clone();
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        let mut errors = Vec::new();
        let mut exited = match child.wait_timeout_without_reaping(remaining) {
            Ok(exited) => exited,
            Err(error) => {
                errors.push(error);
                false
            }
        };
        if !exited {
            match self.should_wait_for_relay_retirement(allowance) {
                Ok(true) => {
                    match child.wait_timeout_without_reaping(
                        retirement_deadline.saturating_duration_since(Instant::now()),
                    ) {
                        Ok(observed) => exited = observed,
                        Err(error) => errors.push(error),
                    }
                }
                Ok(false) => {}
                Err(error) => errors.push(error),
            }
        }
        if !exited {
            if let Err(error) = child.request_retirement() {
                errors.push(error);
            }
            match child.wait_timeout_without_reaping(LAUNCHER_RETIREMENT_GRACE) {
                Ok(observed) => exited = observed,
                Err(error) => errors.push(error),
            }
        }
        match self.operation.has_failure() {
            Ok(failed) => child.failure_recovery_expected = failed,
            Err(error) => errors.push(error),
        }
        if exited {
            if let Err(error) = child.reap() {
                errors.push(error);
            }
        } else {
            errors.push(format!(
                "worker launcher did not retire within {} ms",
                LAUNCHER_RETIREMENT_GRACE.as_millis()
            ));
            if let Err(error) = child.force_stop_inner() {
                errors.push(error);
            }
        }
        let result = if errors.is_empty() {
            Ok(())
        } else {
            Err(errors.join("; additionally "))
        };
        child.finish_retirement(result)
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
    fn mark_ready(&self) -> Result<(), String> {
        self.child
            .lock()
            .map_err(|_| "worker child lock poisoned".to_string())?
            .ready_committed = true;
        Ok(())
    }

    fn commands(&self) -> RelayCommandSender {
        self.commands.clone()
    }

    fn finish_tasks(&mut self) -> Result<Option<WorkerProcessOutcome>, String> {
        // Join only after HUP proves that neither the launcher nor a surviving
        // sandbox descendant can keep the relay protocol stream open.
        let cleanup = {
            let mut child = self
                .child
                .lock()
                .map_err(|_| "worker child lock poisoned".to_string())?;
            if child.is_reaped() {
                Ok(())
            } else {
                child.force_stop()
            }
        };
        let tasks = self.tasks.take();
        let output_closed = tasks.as_ref().map_or(Ok(false), |tasks| {
            relay_stdout_closed(&tasks.relay_stdout_observer)
        });
        let tasks = match (tasks, output_closed) {
            (Some(tasks), Ok(false)) => {
                let RelayTasks {
                    dispatcher,
                    command_writer,
                    event_reader,
                    relay_stdout_observer: _,
                } = *tasks;
                drop(command_writer.stop());
                drop(dispatcher);
                drop(event_reader);
                Ok(None)
            }
            (Some(tasks), Ok(true)) => {
                let command_writer =
                    join_worker_thread(tasks.command_writer.stop(), "relay command writer");
                let event_reader = join_worker_thread(tasks.event_reader, "relay event reader");
                let outcome = tasks.dispatcher.join();
                command_writer.and(event_reader).and(outcome)
            }
            (Some(tasks), Err(error)) => {
                let RelayTasks {
                    dispatcher,
                    command_writer,
                    event_reader,
                    relay_stdout_observer: _,
                } = *tasks;
                drop(command_writer.stop());
                drop(dispatcher);
                drop(event_reader);
                Err(error)
            }
            (None, _) => Ok(None),
        };
        match (tasks, cleanup) {
            (Ok(outcome), Ok(())) => Ok(outcome),
            (Err(error), Ok(())) | (Ok(_), Err(error)) => Err(error),
            (Err(error), Err(cleanup_error)) => Err(format!(
                "{error}; additionally failed to retire sandbox lifetime: {cleanup_error}"
            )),
        }
    }
}

fn relay_stdout_closed(descriptor: &OwnedFd) -> Result<bool, String> {
    let mut event = libc::pollfd {
        fd: descriptor.as_raw_fd(),
        events: libc::POLLIN | libc::POLLHUP,
        revents: 0,
    };
    let result = loop {
        let result = unsafe { libc::poll(&mut event, 1, 0) };
        if result >= 0 {
            break result;
        }
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::EINTR) {
            return Err(format!("failed to inspect worker relay stdout: {error}"));
        }
    };
    if event.revents & libc::POLLNVAL != 0 {
        return Err("worker relay stdout descriptor became invalid".to_string());
    }
    Ok(result > 0 && event.revents & libc::POLLHUP != 0)
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
