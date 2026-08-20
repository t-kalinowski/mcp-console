use std::ffi::OsString;

#[cfg(not(target_os = "macos"))]
pub(crate) fn run(_command_line: &[OsString]) -> Result<(), String> {
    Err("the worker relay is currently supported only on macOS".to_string())
}

#[cfg(target_os = "macos")]
pub(crate) fn run(command_line: &[OsString]) -> Result<(), String> {
    platform::run(command_line)
}

#[cfg(target_os = "macos")]
mod platform {
    use std::collections::VecDeque;
    use std::io::{Read, Write};
    use std::os::fd::{AsRawFd, RawFd};
    use std::os::unix::process::ExitStatusExt as _;
    use std::process::{Child, Command, ExitStatus, Stdio};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Condvar, Mutex, mpsc};
    use std::thread;
    use std::time::{Duration, Instant};

    use wait_timeout::ChildExt as _;

    use crate::relay_protocol::{
        EncodedBytes, JsonlWriter, RelayCommand, RelayEvent, RelayEventPayload, RelayExitStatus,
        RelayStream,
    };
    use crate::worker_protocol::{ServerMessage, WorkerMessage};

    const READ_CHUNK_SIZE: usize = 8 * 1024;
    const WORKER_SHUTDOWN_GRACE: Duration = Duration::from_secs(1);

    pub(super) fn run(command_line: &[std::ffi::OsString]) -> Result<(), String> {
        let (program, arguments) = command_line
            .split_first()
            .ok_or_else(|| "worker relay command must include an executable".to_string())?;

        let (controls, control_receiver) = mpsc::channel();
        let (events, event_writer) = start_event_writer(controls.clone());
        let failures = FailureReporter::new(controls.clone());
        let stopping = Arc::new(AtomicBool::new(false));

        let (sideband_reader, sideband_writer, child_fds) = match crate::sideband::bind() {
            Ok(sideband) => sideband,
            Err(error) => {
                return report_startup_failure(
                    &events,
                    event_writer,
                    format!("failed to create worker sideband: {error}"),
                );
            }
        };
        let mut command = Command::new(program);
        command
            .args(arguments)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        child_fds.configure_process(&mut command);
        let child = match command.spawn() {
            Ok(child) => child,
            Err(error) => {
                return report_startup_failure(
                    &events,
                    event_writer,
                    format!("failed to launch worker: {error}"),
                );
            }
        };
        drop(child_fds);

        let mut child = ChildGuard::new(child);

        // Startup failures below can race worker output from partially started
        // I/O tasks. They must not use the pre-I/O reporter without the normal
        // joined output drain.

        let stdin = child
            .child_mut()
            .stdin
            .take()
            .expect("piped worker stdin should be available");
        let stdout = child
            .child_mut()
            .stdout
            .take()
            .expect("piped worker stdout should be available");
        let stderr = child
            .child_mut()
            .stderr
            .take()
            .expect("piped worker stderr should be available");

        let stdin = StdinWriter::start(stdin, failures.clone())?;
        let sideband_writer =
            SidebandWriter::start(sideband_writer, failures.clone(), stopping.clone());
        let stdout = OutputReader::start(
            stdout,
            RelayStream::Stdout,
            events.clone(),
            failures.clone(),
        )?;
        let stderr = OutputReader::start(
            stderr,
            RelayStream::Stderr,
            events.clone(),
            failures.clone(),
        )?;
        let acknowledgments = Acknowledgments::default();
        let sideband_reader = SidebandReader::start(
            sideband_reader,
            events.clone(),
            acknowledgments.clone(),
            stdout.checkpoint_handle(),
            stderr.checkpoint_handle(),
            failures.clone(),
            controls.clone(),
        )?;
        let command_reader = CommandReader::start(
            sideband_writer.sender(),
            stdin.sender(),
            acknowledgments.clone(),
            events.clone(),
            controls.clone(),
            failures.clone(),
            stopping.clone(),
        )?;

        let mut child = child.take();
        let (status, retirement_error) =
            supervise_worker(&mut child, &control_receiver, &events, &failures, &stopping);
        if let Some(error) = retirement_error.as_ref() {
            failures.report(error.clone());
        }

        stopping.store(true, Ordering::SeqCst);
        let mut finish_error = None;
        collect_error(&mut finish_error, command_reader.cancel_and_join());
        acknowledgments.cancel();
        collect_error(&mut finish_error, stdin.cancel_and_join());
        collect_error(&mut finish_error, sideband_writer.cancel_and_join());
        collect_error(&mut finish_error, sideband_reader.cancel_and_join());
        collect_error(&mut finish_error, stdout.cancel_and_join());
        collect_error(&mut finish_error, stderr.cancel_and_join());

        if let Some(message) = failures.take() {
            collect_error(
                &mut finish_error,
                events.send(RelayEventPayload::Fatal { message }),
            );
        }
        collect_error(
            &mut finish_error,
            events.send(RelayEventPayload::WorkerSidebandClosed),
        );
        if let Some(status) = status {
            collect_error(
                &mut finish_error,
                events.send(RelayEventPayload::WorkerExited {
                    status: RelayExitStatus {
                        code: status.code(),
                        signal: status.signal(),
                    },
                }),
            );
        }
        collect_error(&mut finish_error, events.finish());
        match event_writer.join() {
            Ok(result) => collect_error(&mut finish_error, result),
            Err(_) => collect_error(
                &mut finish_error,
                Err("relay event writer task failed".to_string()),
            ),
        }

        collect_error(&mut finish_error, retirement_error.map_or(Ok(()), Err));
        finish_error.map_or(Ok(()), Err)
    }

    fn collect_error(current: &mut Option<String>, result: Result<(), String>) {
        let Err(error) = result else {
            return;
        };
        match current {
            Some(current) => current.push_str(&format!("; additionally {error}")),
            None => *current = Some(error),
        }
    }

    fn report_startup_failure(
        events: &EventSender,
        event_writer: thread::JoinHandle<Result<(), String>>,
        error: String,
    ) -> Result<(), String> {
        let _ = events.send_confirmed(RelayEventPayload::Fatal {
            message: error.clone(),
        });
        let _ = events.finish();
        let _ = event_writer.join();
        Err(error)
    }

    fn supervise_worker(
        child: &mut Child,
        controls: &mpsc::Receiver<Control>,
        events: &EventSender,
        failures: &FailureReporter,
        stopping: &AtomicBool,
    ) -> (Option<ExitStatus>, Option<String>) {
        loop {
            match controls.recv() {
                Ok(Control::Interrupt { request_id }) => {
                    let error = interrupt_worker(child).err();
                    if events
                        .send(RelayEventPayload::InterruptResult { request_id, error })
                        .is_err()
                    {
                        return force_stop_worker(child, "relay event writer stopped".to_string());
                    }
                }
                Ok(Control::Shutdown { deadline }) => {
                    stopping.store(true, Ordering::SeqCst);
                    return stop_worker(child, deadline, failures);
                }
                Ok(Control::Stop { message }) => {
                    stopping.store(true, Ordering::SeqCst);
                    return force_stop_worker(child, message);
                }
                Ok(Control::SidebandClosed) => {
                    stopping.store(true, Ordering::SeqCst);
                    return match child.try_wait() {
                        Ok(Some(status)) => (Some(status), None),
                        Ok(None) => force_stop_worker(child, String::new()),
                        Err(error) => force_stop_worker(
                            child,
                            format!("failed to read worker status: {error}"),
                        ),
                    };
                }
                Err(_) => {
                    return force_stop_worker(child, "relay control channel stopped".to_string());
                }
            }
        }
    }

    fn interrupt_worker(child: &mut Child) -> Result<(), String> {
        if child
            .try_wait()
            .map_err(|error| format!("failed to read worker status: {error}"))?
            .is_some()
        {
            return Err("worker is not running".to_string());
        }
        // SAFETY: the direct child remains unreaped here, so its PID cannot be
        // reused before `kill` returns.
        if unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGINT) } == 0 {
            Ok(())
        } else {
            Err(format!(
                "failed to interrupt worker: {}",
                std::io::Error::last_os_error()
            ))
        }
    }

    fn stop_worker(
        child: &mut Child,
        deadline: Instant,
        failures: &FailureReporter,
    ) -> (Option<ExitStatus>, Option<String>) {
        let remaining = deadline.saturating_duration_since(Instant::now());
        match child.wait_timeout(remaining) {
            Ok(Some(status)) => (Some(status), None),
            Ok(None) => force_stop_worker(child, String::new()),
            Err(error) => {
                let error = format!("failed to wait for worker to exit: {error}");
                failures.report(error.clone());
                force_stop_worker(child, error)
            }
        }
    }

    fn force_stop_worker(
        child: &mut Child,
        prior_error: String,
    ) -> (Option<ExitStatus>, Option<String>) {
        let mut errors = Vec::new();
        if !prior_error.is_empty() {
            errors.push(prior_error);
        }
        let (mut status, should_kill_direct_worker) = match child.try_wait() {
            Ok(status @ Some(_)) => (status, false),
            Ok(None) => (None, true),
            Err(error) => {
                errors.push(format!("failed to read direct worker status: {error}"));
                (None, false)
            }
        };
        if should_kill_direct_worker
            && let Err(error) = child.kill()
            && error.raw_os_error() != Some(libc::ESRCH)
        {
            errors.push(format!("failed to stop the direct worker: {error}"));
        }
        let group_error = crate::sandbox::force_stop_process_group_members_except_self().err();
        if let Some(error) = group_error.as_ref() {
            errors.push(format!("failed to stop the worker process group: {error}"));
        }
        if status.is_none() {
            match child.wait() {
                Ok(exit_status) => status = Some(exit_status),
                Err(error) => {
                    errors.push(format!("failed to reap the direct worker: {error}"));
                }
            }
        }
        let error = (!errors.is_empty()).then(|| errors.join("; "));
        (status, error)
    }

    struct ChildGuard(Option<Child>);

    impl ChildGuard {
        fn new(child: Child) -> Self {
            Self(Some(child))
        }

        fn child_mut(&mut self) -> &mut Child {
            self.0.as_mut().expect("worker child should be present")
        }

        fn take(mut self) -> Child {
            self.0.take().expect("worker child should be present")
        }
    }

    impl Drop for ChildGuard {
        fn drop(&mut self) {
            if let Some(child) = self.0.as_mut() {
                let _ =
                    force_stop_worker(child, "relay failed while starting worker I/O".to_string());
            }
        }
    }

    #[derive(Clone)]
    struct EventSender(mpsc::Sender<EventRequest>);

    enum EventRequest {
        Send {
            payload: RelayEventPayload,
            confirmation: Option<mpsc::SyncSender<Result<u64, String>>>,
        },
        Finish,
    }

    fn start_event_writer(
        controls: mpsc::Sender<Control>,
    ) -> (EventSender, thread::JoinHandle<Result<(), String>>) {
        let (sender, receiver) = mpsc::channel();
        let thread = thread::spawn(move || {
            let stdout = std::io::stdout();
            let mut writer = JsonlWriter::new(stdout.lock());
            let mut sequence = 0_u64;
            for request in receiver {
                match request {
                    EventRequest::Send {
                        payload,
                        confirmation,
                    } => {
                        let event = RelayEvent { sequence, payload };
                        let result = writer
                            .send(&event)
                            .map(|()| sequence)
                            .map_err(|error| format!("relay stdout write failed: {error}"));
                        if let Some(confirmation) = confirmation {
                            let _ = confirmation.send(result.clone());
                        }
                        match result {
                            Ok(_) => sequence = sequence.wrapping_add(1),
                            Err(error) => {
                                let _ = controls.send(Control::Stop {
                                    message: error.clone(),
                                });
                                return Err(error);
                            }
                        }
                    }
                    EventRequest::Finish => return Ok(()),
                }
            }
            Ok(())
        });
        (EventSender(sender), thread)
    }

    impl EventSender {
        fn send(&self, payload: RelayEventPayload) -> Result<(), String> {
            self.0
                .send(EventRequest::Send {
                    payload,
                    confirmation: None,
                })
                .map_err(|_| "relay event writer stopped".to_string())
        }

        fn send_confirmed(&self, payload: RelayEventPayload) -> Result<u64, String> {
            let (confirmation, receiver) = mpsc::sync_channel(0);
            self.0
                .send(EventRequest::Send {
                    payload,
                    confirmation: Some(confirmation),
                })
                .map_err(|_| "relay event writer stopped".to_string())?;
            receiver
                .recv()
                .map_err(|_| "relay event writer stopped".to_string())?
        }

        fn finish(&self) -> Result<(), String> {
            self.0
                .send(EventRequest::Finish)
                .map_err(|_| "relay event writer stopped".to_string())
        }
    }

    #[derive(Clone)]
    struct FailureReporter {
        controls: mpsc::Sender<Control>,
        message: Arc<Mutex<Option<String>>>,
    }

    impl FailureReporter {
        fn new(controls: mpsc::Sender<Control>) -> Self {
            Self {
                controls,
                message: Arc::new(Mutex::new(None)),
            }
        }

        fn report(&self, message: String) {
            let first = {
                let mut reported = self
                    .message
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                if reported.is_some() {
                    false
                } else {
                    *reported = Some(message.clone());
                    true
                }
            };
            if !first {
                return;
            }
            let _ = self.controls.send(Control::Stop { message });
        }

        fn take(&self) -> Option<String> {
            self.message
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .take()
        }
    }

    enum Control {
        Interrupt { request_id: u64 },
        Shutdown { deadline: Instant },
        SidebandClosed,
        Stop { message: String },
    }

    #[derive(Clone, Default)]
    struct Acknowledgments(Arc<(Mutex<AcknowledgmentState>, Condvar)>);

    #[derive(Default)]
    struct AcknowledgmentState {
        sequences: VecDeque<u64>,
        cancelled: bool,
    }

    impl Acknowledgments {
        fn acknowledge(&self, sequence: u64) {
            let (state, changed) = &*self.0;
            let mut state = state
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            state.sequences.push_back(sequence);
            changed.notify_one();
        }

        fn wait(&self, expected: u64) -> Result<bool, String> {
            let (state, changed) = &*self.0;
            let mut state = state
                .lock()
                .map_err(|_| "relay acknowledgment lock poisoned".to_string())?;
            loop {
                if state.cancelled {
                    return Ok(false);
                }
                if let Some(sequence) = state.sequences.pop_front() {
                    return if sequence == expected {
                        Ok(true)
                    } else {
                        Err(format!(
                            "relay received acknowledgment {sequence} while waiting for {expected}"
                        ))
                    };
                }
                state = changed
                    .wait(state)
                    .map_err(|_| "relay acknowledgment lock poisoned".to_string())?;
            }
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

    struct CommandReader {
        cancel: Cancellation,
        thread: thread::JoinHandle<()>,
    }

    impl CommandReader {
        #[allow(clippy::too_many_arguments)]
        fn start(
            sideband: mpsc::Sender<SidebandWrite>,
            stdin: mpsc::Sender<StdinWrite>,
            acknowledgments: Acknowledgments,
            events: EventSender,
            controls: mpsc::Sender<Control>,
            failures: FailureReporter,
            stopping: Arc<AtomicBool>,
        ) -> Result<Self, String> {
            let (cancelled, cancel) = cancellation_pipe("relay stdin")?;
            let thread = thread::spawn(move || {
                let mut input = std::io::stdin();
                let mut buffer = Vec::new();
                loop {
                    let ready = match wait_for_io(input.as_raw_fd(), libc::POLLIN, &cancelled) {
                        Ok(ready) => ready,
                        Err(error) => {
                            failures.report(format!("relay stdin read failed: {error}"));
                            return;
                        }
                    };
                    if ready.cancelled {
                        return;
                    }
                    if !ready.stream {
                        continue;
                    }
                    let mut chunk = [0_u8; READ_CHUNK_SIZE];
                    match input.read(&mut chunk) {
                        Ok(0) if buffer.is_empty() => {
                            stopping.store(true, Ordering::SeqCst);
                            let _ = stdin.send(StdinWrite::Close);
                            let _ = sideband.send(SidebandWrite::Message(ServerMessage::Shutdown));
                            let _ = controls.send(Control::Shutdown {
                                deadline: Instant::now() + WORKER_SHUTDOWN_GRACE,
                            });
                            return;
                        }
                        Ok(0) => {
                            failures
                                .report("relay stdin closed midway through a frame".to_string());
                            return;
                        }
                        Ok(length) => buffer.extend_from_slice(&chunk[..length]),
                        Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                        Err(error) => {
                            failures.report(format!("relay stdin read failed: {error}"));
                            return;
                        }
                    }
                    while let Some(newline) = buffer.iter().position(|byte| *byte == b'\n') {
                        let mut frame = buffer.drain(..=newline).collect::<Vec<_>>();
                        frame.pop();
                        if frame.last() == Some(&b'\r') {
                            frame.pop();
                        }
                        let command = match serde_json::from_slice::<RelayCommand>(&frame) {
                            Ok(command) => command,
                            Err(error) => {
                                failures.report(format!("relay stdin frame is invalid: {error}"));
                                return;
                            }
                        };
                        match command {
                            RelayCommand::WorkerMessage { message } => {
                                if sideband.send(SidebandWrite::Message(message)).is_err() {
                                    failures.report("worker sideband writer stopped".to_string());
                                    return;
                                }
                            }
                            RelayCommand::Stdin { data } => match data.decode() {
                                Ok(data) => {
                                    if stdin.send(StdinWrite::Write(data)).is_err() {
                                        failures.report("worker stdin writer stopped".to_string());
                                        return;
                                    }
                                }
                                Err(error) => {
                                    failures.report(error);
                                    return;
                                }
                            },
                            RelayCommand::Interrupt { request_id } => {
                                if controls.send(Control::Interrupt { request_id }).is_err() {
                                    let _ = events.send(RelayEventPayload::InterruptResult {
                                        request_id,
                                        error: Some("relay supervisor stopped".to_string()),
                                    });
                                    return;
                                }
                            }
                            RelayCommand::Shutdown { grace_millis } => {
                                let deadline = Instant::now() + Duration::from_millis(grace_millis);
                                if events
                                    .send_confirmed(RelayEventPayload::ShutdownStarted)
                                    .is_err()
                                {
                                    failures.report("relay event writer stopped".to_string());
                                    return;
                                }
                                stopping.store(true, Ordering::SeqCst);
                                let _ = stdin.send(StdinWrite::Close);
                                let _ =
                                    sideband.send(SidebandWrite::Message(ServerMessage::Shutdown));
                                let _ = controls.send(Control::Shutdown { deadline });
                                return;
                            }
                            RelayCommand::Acknowledge { sequence } => {
                                acknowledgments.acknowledge(sequence);
                            }
                        }
                    }
                    buffer.shrink_to(READ_CHUNK_SIZE);
                }
            });
            Ok(Self { cancel, thread })
        }

        fn cancel_and_join(self) -> Result<(), String> {
            self.cancel.cancel();
            self.thread
                .join()
                .map_err(|_| "relay stdin reader task failed".to_string())
        }
    }

    struct SidebandWriter {
        sender: mpsc::Sender<SidebandWrite>,
        thread: thread::JoinHandle<()>,
    }

    enum SidebandWrite {
        Message(ServerMessage),
        Close,
    }

    impl SidebandWriter {
        fn start(
            writer: crate::sideband::Writer,
            failures: FailureReporter,
            stopping: Arc<AtomicBool>,
        ) -> Self {
            let (sender, receiver) = mpsc::channel();
            let thread = thread::spawn(move || {
                for message in receiver {
                    match message {
                        SidebandWrite::Message(message) => {
                            if let Err(error) = writer.send(&message) {
                                if !stopping.load(Ordering::SeqCst) {
                                    failures
                                        .report(format!("worker sideband write failed: {error}"));
                                }
                                return;
                            }
                        }
                        SidebandWrite::Close => return,
                    }
                }
            });
            Self { sender, thread }
        }

        fn sender(&self) -> mpsc::Sender<SidebandWrite> {
            self.sender.clone()
        }

        fn cancel_and_join(self) -> Result<(), String> {
            let _ = self.sender.send(SidebandWrite::Close);
            self.thread
                .join()
                .map_err(|_| "worker sideband writer task failed".to_string())
        }
    }

    struct SidebandReader {
        cancel: Cancellation,
        thread: thread::JoinHandle<()>,
    }

    impl SidebandReader {
        fn start(
            mut reader: crate::sideband::Reader,
            events: EventSender,
            acknowledgments: Acknowledgments,
            stdout: OutputCheckpoint,
            stderr: OutputCheckpoint,
            failures: FailureReporter,
            controls: mpsc::Sender<Control>,
        ) -> Result<Self, String> {
            set_nonblocking(&reader)?;
            let (cancelled, cancel) = cancellation_pipe("worker sideband")?;
            let thread = thread::spawn(move || {
                let mut ordinary_close = false;
                let mut sideband_failure = None;
                'read: loop {
                    while let Some(message) = match reader.receive_buffered() {
                        Ok(message) => message,
                        Err(error) => {
                            sideband_failure =
                                Some(format!("worker sideband read failed: {error}"));
                            break 'read;
                        }
                    } {
                        let terminal = worker_message_requires_acknowledgment(&message);
                        if let Err(error) = stdout.checkpoint().and_then(|()| stderr.checkpoint()) {
                            failures.report(error);
                            break 'read;
                        }
                        let sequence = if terminal {
                            match events
                                .send_confirmed(RelayEventPayload::WorkerMessage { message })
                            {
                                Ok(sequence) => sequence,
                                Err(error) => {
                                    failures.report(error);
                                    break 'read;
                                }
                            }
                        } else {
                            if let Err(error) =
                                events.send(RelayEventPayload::WorkerMessage { message })
                            {
                                failures.report(error);
                                break 'read;
                            }
                            continue;
                        };
                        match acknowledgments.wait(sequence) {
                            Ok(true) => {}
                            Ok(false) => break 'read,
                            Err(error) => {
                                failures.report(error);
                                break 'read;
                            }
                        }
                    }

                    let ready = match wait_for_io(reader.as_raw_fd(), libc::POLLIN, &cancelled) {
                        Ok(ready) => ready,
                        Err(error) => {
                            sideband_failure =
                                Some(format!("worker sideband read failed: {error}"));
                            break;
                        }
                    };
                    if ready.cancelled {
                        break;
                    }
                    if !ready.stream {
                        continue;
                    }
                    let had_buffered_data = reader.has_buffered_data();
                    match reader.read_chunk() {
                        Ok(()) => {}
                        Err(error)
                            if matches!(
                                error.kind(),
                                std::io::ErrorKind::Interrupted | std::io::ErrorKind::WouldBlock
                            ) => {}
                        Err(error)
                            if error.kind() == std::io::ErrorKind::UnexpectedEof
                                && !had_buffered_data =>
                        {
                            ordinary_close = true;
                            break;
                        }
                        Err(error) => {
                            sideband_failure =
                                Some(format!("worker sideband read failed: {error}"));
                            break;
                        }
                    }
                }
                if let Err(error) = stdout.checkpoint().and_then(|()| stderr.checkpoint()) {
                    failures.report(error);
                    return;
                }
                if let Some(error) = sideband_failure {
                    failures.report(error);
                }
                if ordinary_close {
                    let _ = controls.send(Control::SidebandClosed);
                }
            });
            Ok(Self { cancel, thread })
        }

        fn cancel_and_join(self) -> Result<(), String> {
            self.cancel.cancel();
            self.thread
                .join()
                .map_err(|_| "worker sideband reader task failed".to_string())
        }
    }

    fn worker_message_requires_acknowledgment(message: &WorkerMessage) -> bool {
        matches!(
            message,
            WorkerMessage::Completed
                | WorkerMessage::RPrepared { .. }
                | WorkerMessage::RPreparationFailed { .. }
                | WorkerMessage::PythonPrepared
                | WorkerMessage::PythonPreparationFailed { .. }
        )
    }

    struct StdinWriter {
        sender: mpsc::Sender<StdinWrite>,
        cancel: Cancellation,
        thread: thread::JoinHandle<()>,
    }

    enum StdinWrite {
        Write(Vec<u8>),
        Close,
    }

    impl StdinWriter {
        fn start(
            mut stream: std::process::ChildStdin,
            failures: FailureReporter,
        ) -> Result<Self, String> {
            set_nonblocking(&stream)?;
            let (cancelled, cancel) = cancellation_pipe("worker stdin")?;
            let (sender, receiver) = mpsc::channel();
            let thread = thread::spawn(move || {
                for message in receiver {
                    match message {
                        StdinWrite::Write(bytes) => {
                            let mut remaining = bytes.as_slice();
                            while !remaining.is_empty() {
                                let ready = match wait_for_io(
                                    stream.as_raw_fd(),
                                    libc::POLLOUT,
                                    &cancelled,
                                ) {
                                    Ok(ready) => ready,
                                    Err(error) => {
                                        failures
                                            .report(format!("worker stdin write failed: {error}"));
                                        return;
                                    }
                                };
                                if ready.cancelled {
                                    return;
                                }
                                if !ready.stream {
                                    continue;
                                }
                                match stream.write(remaining) {
                                    Ok(0) => {
                                        failures.report(
                                            "worker stdin write failed: write returned zero bytes"
                                                .to_string(),
                                        );
                                        return;
                                    }
                                    Ok(length) => remaining = &remaining[length..],
                                    Err(error)
                                        if matches!(
                                            error.kind(),
                                            std::io::ErrorKind::Interrupted
                                                | std::io::ErrorKind::WouldBlock
                                        ) => {}
                                    Err(error) => {
                                        failures
                                            .report(format!("worker stdin write failed: {error}"));
                                        return;
                                    }
                                }
                            }
                        }
                        StdinWrite::Close => return,
                    }
                }
            });
            Ok(Self {
                sender,
                cancel,
                thread,
            })
        }

        fn sender(&self) -> mpsc::Sender<StdinWrite> {
            self.sender.clone()
        }

        fn cancel_and_join(self) -> Result<(), String> {
            let _ = self.sender.send(StdinWrite::Close);
            self.cancel.cancel();
            self.thread
                .join()
                .map_err(|_| "worker stdin writer task failed".to_string())
        }
    }

    struct OutputReader {
        checkpoint: OutputCheckpoint,
        thread: thread::JoinHandle<()>,
    }

    #[derive(Clone)]
    struct OutputCheckpoint {
        commands: mpsc::Sender<OutputCommand>,
        wake: Cancellation,
    }

    enum OutputCommand {
        Checkpoint(mpsc::SyncSender<()>),
        Stop,
    }

    impl OutputReader {
        fn start(
            mut stream: impl Read + AsRawFd + Send + 'static,
            kind: RelayStream,
            events: EventSender,
            failures: FailureReporter,
        ) -> Result<Self, String> {
            set_nonblocking(&stream)?;
            let (woken, wake) = cancellation_pipe("worker output")?;
            let (commands, receiver) = mpsc::channel();
            let checkpoint = OutputCheckpoint { commands, wake };
            let thread = thread::spawn(move || {
                let mut buffer = [0_u8; READ_CHUNK_SIZE];
                'read: loop {
                    let ready = match wait_for_io(stream.as_raw_fd(), libc::POLLIN, &woken) {
                        Ok(ready) => ready,
                        Err(error) => {
                            failures.report(format!("worker output read failed: {error}"));
                            break;
                        }
                    };
                    if ready.stream {
                        match stream.read(&mut buffer) {
                            Ok(0) => break,
                            Ok(length) => {
                                if events.send(output_event(kind, &buffer[..length])).is_err() {
                                    break;
                                }
                            }
                            Err(error)
                                if matches!(
                                    error.kind(),
                                    std::io::ErrorKind::Interrupted
                                        | std::io::ErrorKind::WouldBlock
                                ) => {}
                            Err(error) => {
                                failures.report(format!("worker output read failed: {error}"));
                                break;
                            }
                        }
                    }
                    if ready.cancelled {
                        drain_wakeup(&woken);
                        while let Ok(command) = receiver.try_recv() {
                            match command {
                                OutputCommand::Checkpoint(done) => {
                                    drain_buffered_output(&mut stream, kind, &events, &mut buffer);
                                    let _ = done.send(());
                                }
                                OutputCommand::Stop => {
                                    drain_buffered_output(&mut stream, kind, &events, &mut buffer);
                                    break 'read;
                                }
                            }
                        }
                    }
                }
                let _ = events.send(RelayEventPayload::StreamClosed { stream: kind });
            });
            Ok(Self { checkpoint, thread })
        }

        fn checkpoint_handle(&self) -> OutputCheckpoint {
            self.checkpoint.clone()
        }

        fn cancel_and_join(self) -> Result<(), String> {
            if self.checkpoint.commands.send(OutputCommand::Stop).is_ok() {
                self.checkpoint.wake.wake();
            }
            self.thread
                .join()
                .map_err(|_| "worker output reader task failed".to_string())
        }
    }

    impl OutputCheckpoint {
        fn checkpoint(&self) -> Result<(), String> {
            let (done, receiver) = mpsc::sync_channel(0);
            if self.commands.send(OutputCommand::Checkpoint(done)).is_err() {
                return Ok(());
            }
            self.wake.wake();
            // A concurrent EOF closes the reader only after all stream bytes
            // have been sent, which also satisfies this checkpoint.
            let _ = receiver.recv();
            Ok(())
        }
    }

    fn output_event(stream: RelayStream, bytes: &[u8]) -> RelayEventPayload {
        let data = EncodedBytes::from_bytes(bytes);
        match stream {
            RelayStream::Stdout => RelayEventPayload::Stdout { data },
            RelayStream::Stderr => RelayEventPayload::Stderr { data },
        }
    }

    fn drain_buffered_output(
        stream: &mut (impl Read + AsRawFd),
        kind: RelayStream,
        events: &EventSender,
        buffer: &mut [u8],
    ) {
        let mut remaining: libc::c_int = 0;
        // SAFETY: the stream remains open and `remaining` points to writable
        // storage of the type expected by FIONREAD.
        if unsafe { libc::ioctl(stream.as_raw_fd(), libc::FIONREAD, &mut remaining) } < 0 {
            return;
        }
        let mut remaining = remaining.max(0) as usize;
        while remaining > 0 {
            let length = remaining.min(buffer.len());
            match stream.read(&mut buffer[..length]) {
                Ok(0) => break,
                Ok(length) => {
                    if events.send(output_event(kind, &buffer[..length])).is_err() {
                        break;
                    }
                    remaining -= length;
                }
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => break,
            }
        }
    }

    fn drain_wakeup(reader: &std::io::PipeReader) {
        let mut available: libc::c_int = 0;
        // SAFETY: the wake pipe is open and `available` has the expected type.
        if unsafe { libc::ioctl(reader.as_raw_fd(), libc::FIONREAD, &mut available) } < 0 {
            return;
        }
        let mut remaining = available.max(0) as usize;
        let mut buffer = [0_u8; 64];
        while remaining > 0 {
            let length = remaining.min(buffer.len());
            match (&*reader).read(&mut buffer[..length]) {
                Ok(0) | Err(_) => return,
                Ok(length) => remaining -= length,
            }
        }
    }

    #[derive(Clone)]
    struct Cancellation(Arc<Mutex<Option<std::io::PipeWriter>>>);

    impl Cancellation {
        fn wake(&self) {
            let mut writer = match self.0.lock() {
                Ok(writer) => writer,
                Err(poisoned) => poisoned.into_inner(),
            };
            if let Some(writer) = writer.as_mut() {
                let _ = writer.write_all(&[1]);
            }
        }

        fn cancel(&self) {
            let mut writer = match self.0.lock() {
                Ok(writer) => writer,
                Err(poisoned) => poisoned.into_inner(),
            };
            drop(writer.take());
        }
    }

    fn cancellation_pipe(description: &str) -> Result<(std::io::PipeReader, Cancellation), String> {
        let (reader, writer) = std::io::pipe().map_err(|error| {
            format!("failed to create {description} cancellation pipe: {error}")
        })?;
        Ok((reader, Cancellation(Arc::new(Mutex::new(Some(writer))))))
    }

    struct ReadyIo {
        stream: bool,
        cancelled: bool,
    }

    fn wait_for_io(
        descriptor: RawFd,
        events: libc::c_short,
        cancelled: &std::io::PipeReader,
    ) -> std::io::Result<ReadyIo> {
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
            // SAFETY: both descriptors remain open for this call and the array
            // pointer and length describe initialized storage exactly.
            if unsafe { libc::poll(descriptors.as_mut_ptr(), descriptors.len() as _, -1) } >= 0 {
                return Ok(ReadyIo {
                    stream: descriptors[0].revents != 0,
                    cancelled: descriptors[1].revents != 0,
                });
            }
            let error = std::io::Error::last_os_error();
            if error.kind() != std::io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
    }

    fn set_nonblocking(descriptor: &impl AsRawFd) -> Result<(), String> {
        let descriptor = descriptor.as_raw_fd();
        // SAFETY: `descriptor` is an open pipe owned by the relay.
        let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFL) };
        if flags < 0 {
            return Err(format!(
                "failed to read relay descriptor flags: {}",
                std::io::Error::last_os_error()
            ));
        }
        // SAFETY: this preserves existing flags and adds O_NONBLOCK.
        if unsafe { libc::fcntl(descriptor, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
            return Err(format!(
                "failed to configure relay descriptor: {}",
                std::io::Error::last_os_error()
            ));
        }
        Ok(())
    }
}
