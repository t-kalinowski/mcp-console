use std::ffi::OsString;

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub(crate) fn run(_command_line: &[OsString]) -> Result<(), String> {
    Err("the worker relay is currently supported only on macOS and Linux".to_string())
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
pub(crate) fn run(command_line: &[OsString]) -> Result<(), String> {
    platform::run(command_line)
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
mod platform {
    use std::io::{Read, Write};
    use std::os::fd::{AsRawFd, RawFd};
    use std::os::unix::process::ExitStatusExt as _;
    use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command, ExitStatus, Stdio};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Mutex, mpsc};
    use std::thread;
    use std::time::{Duration, Instant};

    use crate::relay_protocol::{EncodedBytes, JsonlWriter, RelayCommand, RelayEvent};
    use crate::worker_protocol::{ServerMessage, WorkerMessage};

    const READ_CHUNK_SIZE: usize = 8 * 1024;
    const WORKER_SHUTDOWN_GRACE: Duration = Duration::from_secs(1);
    const CHILD_EXITED: libc::c_int = 1;
    const CHILD_KILLED: libc::c_int = 2;
    const CHILD_DUMPED: libc::c_int = 3;
    const CHILD_STOPPED: libc::c_int = 5;
    const CHILD_CONTINUED: libc::c_int = 6;

    pub(super) fn run(command_line: &[std::ffi::OsString]) -> Result<(), String> {
        let (program, arguments) = command_line
            .split_first()
            .ok_or_else(|| "worker relay command must include an executable".to_string())?;

        let (controls, control_receiver) = mpsc::channel();
        let (events, event_writer) = start_event_writer(controls.clone());
        let failures = FailureReporter::new(controls.clone());
        let stopping = Arc::new(AtomicBool::new(false));

        let (sideband_reader, sideband_writer, child_endpoint) = match crate::sideband::bind() {
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
        child_endpoint.configure_process(&mut command);
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
        drop(child_endpoint);

        let mut worker = WorkerLifecycle::new(child);
        let setup = worker.start_io(
            sideband_reader,
            sideband_writer,
            &events,
            &failures,
            &controls,
            &stopping,
        );
        let (status, retirement_error) = match setup {
            Ok(()) => {
                worker.start_exit_watcher(controls.clone());
                let worker_sideband = worker
                    .sideband_writer
                    .as_ref()
                    .expect("worker sideband writer should be running")
                    .sender();
                let worker_stdin = worker
                    .stdin
                    .as_ref()
                    .expect("worker stdin writer should be running")
                    .sender();
                let result = supervise_worker(
                    &mut worker.child,
                    &control_receiver,
                    &events,
                    &stopping,
                    &worker_sideband,
                    &worker_stdin,
                );
                worker.retired = true;
                result
            }
            Err(error) => {
                failures.report(error.clone());
                stopping.store(true, Ordering::SeqCst);
                let result = force_stop_worker(&mut worker.child, error);
                worker.retired = true;
                result
            }
        };
        if let Some(error) = retirement_error.as_ref() {
            failures.report(error.clone());
        }

        stopping.store(true, Ordering::SeqCst);
        let mut finish_error = worker.cancel_and_join(&events);

        collect_error(&mut finish_error, events.send(RelayEvent::StdoutClosed));
        collect_error(&mut finish_error, events.send(RelayEvent::StderrClosed));
        if let Some(message) = failures.take() {
            collect_error(
                &mut finish_error,
                events.send(RelayEvent::Fatal { message }),
            );
        }
        collect_error(
            &mut finish_error,
            events.send(RelayEvent::WorkerSidebandClosed),
        );
        if let Some(status) = status {
            let outcome = match (status.code(), status.signal()) {
                (Some(code), _) => RelayEvent::WorkerExited { code },
                (None, Some(signal)) => RelayEvent::WorkerSignaled { signal },
                (None, None) => RelayEvent::Fatal {
                    message: "worker exited without an exit code or signal".to_string(),
                },
            };
            collect_error(&mut finish_error, events.send(outcome));
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
        let _ = events.send_confirmed(RelayEvent::Fatal {
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
        stopping: &AtomicBool,
        sideband: &mpsc::Sender<SidebandWrite>,
        stdin: &mpsc::Sender<StdinWrite>,
    ) -> (Option<ExitStatus>, Option<String>) {
        let mut exit_deadline: Option<Instant> = None;
        loop {
            let control = match exit_deadline {
                Some(deadline) => {
                    match controls.recv_timeout(deadline.saturating_duration_since(Instant::now()))
                    {
                        Ok(control) => control,
                        Err(mpsc::RecvTimeoutError::Timeout) => {
                            return force_stop_worker(child, String::new());
                        }
                        Err(mpsc::RecvTimeoutError::Disconnected) => {
                            return force_stop_worker(
                                child,
                                "relay control channel stopped".to_string(),
                            );
                        }
                    }
                }
                None => match controls.recv() {
                    Ok(control) => control,
                    Err(_) => {
                        return force_stop_worker(
                            child,
                            "relay control channel stopped".to_string(),
                        );
                    }
                },
            };
            match control {
                Control::Interrupt { request_id } => {
                    let error = interrupt_worker(child).err();
                    if events
                        .send(RelayEvent::InterruptResult { request_id, error })
                        .is_err()
                    {
                        return force_stop_worker(child, "relay event writer stopped".to_string());
                    }
                }
                Control::Shutdown {
                    deadline,
                    report_acceptance,
                } => {
                    if report_acceptance
                        && events.send_confirmed(RelayEvent::ShutdownStarted).is_err()
                    {
                        return force_stop_worker(child, "relay event writer stopped".to_string());
                    }
                    stopping.store(true, Ordering::SeqCst);
                    let _ = stdin.send(StdinWrite::Close);
                    let _ = sideband.send(SidebandWrite::Message(ServerMessage::Shutdown));
                    exit_deadline = Some(deadline);
                }
                Control::Stop { message } => {
                    stopping.store(true, Ordering::SeqCst);
                    return force_stop_worker(child, message);
                }
                Control::SidebandClosed => {
                    stopping.store(true, Ordering::SeqCst);
                    exit_deadline.get_or_insert_with(|| Instant::now() + WORKER_SHUTDOWN_GRACE);
                }
                Control::WorkerExited(result) => {
                    stopping.store(true, Ordering::SeqCst);
                    return match result {
                        Ok(()) => finish_exited_worker(child),
                        Err(error) => force_stop_worker(child, error),
                    };
                }
            }
        }
    }

    fn start_worker_exit_watcher(
        process_id: u32,
        controls: mpsc::Sender<Control>,
    ) -> thread::JoinHandle<()> {
        thread::spawn(move || {
            let process_id = process_id as libc::pid_t;
            let wait_id = process_id as libc::id_t;
            let result = loop {
                let mut information = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
                // SAFETY: `information` points to writable storage and
                // `process_id` names the direct child. WNOWAIT preserves its
                // exit status for the relay supervisor, which remains the sole
                // reaper.
                let result = unsafe {
                    libc::waitid(
                        libc::P_PID,
                        wait_id,
                        information.as_mut_ptr(),
                        libc::WEXITED | libc::WNOWAIT,
                    )
                };
                if result == 0 {
                    // SAFETY: successful `waitid` initialized the supplied
                    // `siginfo_t`.
                    let information = unsafe { information.assume_init() };
                    let observed_process_id = child_status_process_id(&information);
                    if observed_process_id != process_id {
                        break Err(format!(
                            "waitid returned process {} while waiting for worker {process_id}",
                            observed_process_id
                        ));
                    }
                    match information.si_code {
                        CHILD_EXITED | CHILD_KILLED | CHILD_DUMPED => break Ok(()),
                        CHILD_STOPPED | CHILD_CONTINUED => {
                            // Darwin may return a pending stop or continue
                            // notification even though the call requested only
                            // `WEXITED`. Consume just that notification, leaving
                            // the eventual exit status waitable for supervision.
                            if let Err(error) =
                                consume_worker_non_exit_notification(wait_id, process_id)
                            {
                                if error.kind() == std::io::ErrorKind::Interrupted {
                                    continue;
                                }
                                break Err(format!(
                                    "failed to consume worker status notification: {error}"
                                ));
                            }
                        }
                        code => {
                            break Err(format!(
                                "waitid returned unexpected worker status code {code}"
                            ));
                        }
                    }
                    continue;
                }
                let error = std::io::Error::last_os_error();
                if error.kind() != std::io::ErrorKind::Interrupted {
                    break Err(format!("failed to observe worker exit: {error}"));
                }
            };
            let _ = controls.send(Control::WorkerExited(result));
        })
    }

    fn consume_worker_non_exit_notification(
        wait_id: libc::id_t,
        process_id: libc::pid_t,
    ) -> std::io::Result<()> {
        let mut information = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
        // SAFETY: `information` points to writable storage and `process_id`
        // names the direct child. Omitting `WEXITED` and `WNOWAIT` consumes only
        // a pending stop or continue notification, never the exit status.
        let result = unsafe {
            libc::waitid(
                libc::P_PID,
                wait_id,
                information.as_mut_ptr(),
                libc::WSTOPPED | libc::WCONTINUED | libc::WNOHANG,
            )
        };
        if result < 0 {
            return Err(std::io::Error::last_os_error());
        }

        // SAFETY: successful `waitid` initialized the supplied `siginfo_t`.
        let information = unsafe { information.assume_init() };
        let observed_process_id = child_status_process_id(&information);
        if observed_process_id == 0 {
            return Ok(());
        }
        if observed_process_id != process_id {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!(
                    "waitid returned process {} while consuming a notification for worker {process_id}",
                    observed_process_id
                ),
            ));
        }
        if !matches!(information.si_code, CHILD_STOPPED | CHILD_CONTINUED) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!(
                    "waitid consumed unexpected worker status code {}",
                    information.si_code
                ),
            ));
        }
        Ok(())
    }

    fn child_status_process_id(information: &libc::siginfo_t) -> libc::pid_t {
        #[cfg(target_os = "macos")]
        {
            information.si_pid
        }
        #[cfg(target_os = "linux")]
        {
            // SAFETY: callers pass siginfo initialized by a successful waitid.
            unsafe { information.si_pid() }
        }
    }

    fn finish_exited_worker(child: &mut Child) -> (Option<ExitStatus>, Option<String>) {
        match child.wait() {
            Ok(status) => (Some(status), None),
            Err(error) => (
                None,
                Some(format!("failed to reap the direct worker: {error}")),
            ),
        }
    }

    fn interrupt_worker(child: &mut Child) -> Result<(), String> {
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

    struct WorkerLifecycle {
        child: Child,
        retired: bool,
        raw_stdin: Option<ChildStdin>,
        raw_stdout: Option<ChildStdout>,
        raw_stderr: Option<ChildStderr>,
        raw_sideband_reader: Option<crate::sideband::Reader>,
        stdin: Option<StdinWriter>,
        sideband_writer: Option<SidebandWriter>,
        stdout: Option<OutputReader>,
        stderr: Option<OutputReader>,
        sideband_reader: Option<SidebandReader>,
        command_reader: Option<CommandReader>,
        exit_watcher: Option<thread::JoinHandle<()>>,
    }

    impl WorkerLifecycle {
        fn new(mut child: Child) -> Self {
            let raw_stdin = child
                .stdin
                .take()
                .expect("piped worker stdin should be available");
            let raw_stdout = child
                .stdout
                .take()
                .expect("piped worker stdout should be available");
            let raw_stderr = child
                .stderr
                .take()
                .expect("piped worker stderr should be available");
            Self {
                child,
                retired: false,
                raw_stdin: Some(raw_stdin),
                raw_stdout: Some(raw_stdout),
                raw_stderr: Some(raw_stderr),
                raw_sideband_reader: None,
                stdin: None,
                sideband_writer: None,
                stdout: None,
                stderr: None,
                sideband_reader: None,
                command_reader: None,
                exit_watcher: None,
            }
        }

        #[allow(clippy::too_many_arguments)]
        fn start_io(
            &mut self,
            sideband_reader: crate::sideband::Reader,
            sideband_writer: crate::sideband::Writer,
            events: &EventSender,
            failures: &FailureReporter,
            controls: &mpsc::Sender<Control>,
            stopping: &Arc<AtomicBool>,
        ) -> Result<(), String> {
            self.raw_sideband_reader = Some(sideband_reader);
            let stdin = self
                .raw_stdin
                .take()
                .expect("raw worker stdin should be available");
            self.stdin = Some(StdinWriter::start(
                stdin,
                failures.clone(),
                stopping.clone(),
            )?);
            self.sideband_writer = Some(SidebandWriter::start(
                sideband_writer,
                failures.clone(),
                stopping.clone(),
            ));

            let stdout = self
                .raw_stdout
                .take()
                .expect("raw worker stdout should be available");
            match OutputReader::start(
                stdout,
                OutputStream::Stdout,
                events.clone(),
                failures.clone(),
            ) {
                Ok(stdout) => self.stdout = Some(stdout),
                Err((stdout, error)) => {
                    self.raw_stdout = Some(stdout);
                    return Err(error);
                }
            }

            let stderr = self
                .raw_stderr
                .take()
                .expect("raw worker stderr should be available");
            match OutputReader::start(
                stderr,
                OutputStream::Stderr,
                events.clone(),
                failures.clone(),
            ) {
                Ok(stderr) => self.stderr = Some(stderr),
                Err((stderr, error)) => {
                    self.raw_stderr = Some(stderr);
                    return Err(error);
                }
            }

            self.command_reader = Some(CommandReader::start(
                self.sideband_writer
                    .as_ref()
                    .expect("worker sideband writer should be running")
                    .sender(),
                self.stdin
                    .as_ref()
                    .expect("worker stdin writer should be running")
                    .sender(),
                controls.clone(),
                failures.clone(),
            )?);

            let sideband_reader = self
                .raw_sideband_reader
                .take()
                .expect("raw worker sideband reader should be available");
            match SidebandReader::start(
                sideband_reader,
                events.clone(),
                failures.clone(),
                controls.clone(),
            ) {
                Ok(sideband_reader) => self.sideband_reader = Some(sideband_reader),
                Err((sideband_reader, error)) => {
                    self.raw_sideband_reader = Some(sideband_reader);
                    return Err(error);
                }
            }
            Ok(())
        }

        fn start_exit_watcher(&mut self, controls: mpsc::Sender<Control>) {
            self.exit_watcher = Some(start_worker_exit_watcher(self.child.id(), controls));
        }

        fn cancel_and_join(&mut self, events: &EventSender) -> Option<String> {
            let mut error = None;
            drop(self.raw_stdin.take());
            if let Some(command_reader) = self.command_reader.take() {
                collect_error(&mut error, command_reader.cancel_and_join());
            }
            if let Some(stdin) = self.stdin.take() {
                collect_error(&mut error, stdin.cancel_and_join());
            }
            if let Some(sideband_writer) = self.sideband_writer.take() {
                collect_error(&mut error, sideband_writer.cancel_and_join());
            }
            match (self.sideband_reader.take(), self.raw_sideband_reader.take()) {
                (Some(sideband_reader), None) => {
                    collect_error(&mut error, sideband_reader.cancel_and_join())
                }
                (None, Some(mut sideband_reader)) => {
                    collect_error(&mut error, discard_retiring_sideband(&mut sideband_reader));
                }
                _ => unreachable!("worker sideband reader must have exactly one owner"),
            }
            match (self.stdout.take(), self.raw_stdout.take()) {
                (Some(stdout), None) => collect_error(&mut error, stdout.cancel_and_join()),
                (None, Some(stdout)) => collect_error(
                    &mut error,
                    drain_unstarted_output(stdout, OutputStream::Stdout, events),
                ),
                _ => unreachable!("worker stdout must have exactly one owner"),
            }
            match (self.stderr.take(), self.raw_stderr.take()) {
                (Some(stderr), None) => collect_error(&mut error, stderr.cancel_and_join()),
                (None, Some(stderr)) => collect_error(
                    &mut error,
                    drain_unstarted_output(stderr, OutputStream::Stderr, events),
                ),
                _ => unreachable!("worker stderr must have exactly one owner"),
            }
            if self
                .exit_watcher
                .take()
                .is_some_and(|watcher| watcher.join().is_err())
            {
                collect_error(
                    &mut error,
                    Err("worker exit watcher task failed".to_string()),
                );
            }
            error
        }
    }

    impl Drop for WorkerLifecycle {
        fn drop(&mut self) {
            if !self.retired {
                let _ = force_stop_worker(
                    &mut self.child,
                    "relay failed while starting worker I/O".to_string(),
                );
            }
        }
    }

    #[derive(Clone)]
    struct EventSender(mpsc::Sender<EventRequest>);

    enum EventRequest {
        Send {
            event: Box<RelayEvent>,
            confirmation: Option<mpsc::SyncSender<Result<(), String>>>,
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
            for request in receiver {
                match request {
                    EventRequest::Send {
                        event,
                        confirmation,
                    } => {
                        let result = writer
                            .send(&event)
                            .map_err(|error| format!("relay stdout write failed: {error}"));
                        if let Some(confirmation) = confirmation {
                            let _ = confirmation.send(result.clone());
                        }
                        match result {
                            Ok(()) => {}
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
        fn send(&self, event: RelayEvent) -> Result<(), String> {
            self.0
                .send(EventRequest::Send {
                    event: Box::new(event),
                    confirmation: None,
                })
                .map_err(|_| "relay event writer stopped".to_string())
        }

        fn send_confirmed(&self, event: RelayEvent) -> Result<(), String> {
            let (confirmation, receiver) = mpsc::sync_channel(0);
            self.0
                .send(EventRequest::Send {
                    event: Box::new(event),
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
        Interrupt {
            request_id: u64,
        },
        Shutdown {
            deadline: Instant,
            report_acceptance: bool,
        },
        SidebandClosed,
        Stop {
            message: String,
        },
        WorkerExited(Result<(), String>),
    }

    struct CommandReader {
        cancel: Cancellation,
        thread: thread::JoinHandle<()>,
    }

    impl CommandReader {
        fn start(
            sideband: mpsc::Sender<SidebandWrite>,
            stdin: mpsc::Sender<StdinWrite>,
            controls: mpsc::Sender<Control>,
            failures: FailureReporter,
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
                            let _ = controls.send(Control::Shutdown {
                                deadline: Instant::now() + WORKER_SHUTDOWN_GRACE,
                                report_acceptance: false,
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
                            RelayCommand::Evaluate { language, source } => {
                                if sideband
                                    .send(SidebandWrite::Message(ServerMessage::Evaluate {
                                        language,
                                        source,
                                    }))
                                    .is_err()
                                {
                                    failures.report("worker sideband writer stopped".to_string());
                                    return;
                                }
                            }
                            RelayCommand::PrepareR { library } => {
                                if sideband
                                    .send(SidebandWrite::Message(ServerMessage::PrepareR {
                                        library,
                                    }))
                                    .is_err()
                                {
                                    failures.report("worker sideband writer stopped".to_string());
                                    return;
                                }
                            }
                            RelayCommand::RResolved { library } => {
                                if sideband
                                    .send(SidebandWrite::Message(ServerMessage::RResolved {
                                        library,
                                    }))
                                    .is_err()
                                {
                                    failures.report("worker sideband writer stopped".to_string());
                                    return;
                                }
                            }
                            RelayCommand::RResolutionFailed { failure, message } => {
                                if sideband
                                    .send(SidebandWrite::Message(
                                        ServerMessage::RResolutionFailed { failure, message },
                                    ))
                                    .is_err()
                                {
                                    failures.report("worker sideband writer stopped".to_string());
                                    return;
                                }
                            }
                            RelayCommand::PreparePython { packages } => {
                                if sideband
                                    .send(SidebandWrite::Message(ServerMessage::PreparePython {
                                        packages,
                                    }))
                                    .is_err()
                                {
                                    failures.report("worker sideband writer stopped".to_string());
                                    return;
                                }
                            }
                            RelayCommand::PythonResolved { python } => {
                                if sideband
                                    .send(SidebandWrite::Message(ServerMessage::PythonResolved {
                                        python,
                                    }))
                                    .is_err()
                                {
                                    failures.report("worker sideband writer stopped".to_string());
                                    return;
                                }
                            }
                            RelayCommand::PythonResolutionFailed { message } => {
                                if sideband
                                    .send(SidebandWrite::Message(
                                        ServerMessage::PythonResolutionFailed { message },
                                    ))
                                    .is_err()
                                {
                                    failures.report("worker sideband writer stopped".to_string());
                                    return;
                                }
                            }
                            RelayCommand::PythonVersionResolved { version } => {
                                if sideband
                                    .send(SidebandWrite::Message(
                                        ServerMessage::PythonVersionResolved { version },
                                    ))
                                    .is_err()
                                {
                                    failures.report("worker sideband writer stopped".to_string());
                                    return;
                                }
                            }
                            RelayCommand::PythonVersionResolutionFailed { message } => {
                                if sideband
                                    .send(SidebandWrite::Message(
                                        ServerMessage::PythonVersionResolutionFailed { message },
                                    ))
                                    .is_err()
                                {
                                    failures.report("worker sideband writer stopped".to_string());
                                    return;
                                }
                            }
                            RelayCommand::Stdin { data } => {
                                if stdin.send(StdinWrite::Write(data.into_bytes())).is_err() {
                                    failures.report("worker stdin writer stopped".to_string());
                                    return;
                                }
                            }
                            RelayCommand::Interrupt { request_id } => {
                                if controls.send(Control::Interrupt { request_id }).is_err() {
                                    failures.report("relay supervisor stopped".to_string());
                                    return;
                                }
                            }
                            RelayCommand::Shutdown { grace_millis } => {
                                let deadline = Instant::now() + Duration::from_millis(grace_millis);
                                if controls
                                    .send(Control::Shutdown {
                                        deadline,
                                        report_acceptance: true,
                                    })
                                    .is_err()
                                {
                                    failures.report("relay supervisor stopped".to_string());
                                    return;
                                }
                                return;
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
        cancellation: crate::sideband::Writer,
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
            let cancellation = writer.clone();
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
            Self {
                sender,
                cancellation,
                thread,
            }
        }

        fn sender(&self) -> mpsc::Sender<SidebandWrite> {
            self.sender.clone()
        }

        fn cancel_and_join(self) -> Result<(), String> {
            let _ = self.sender.send(SidebandWrite::Close);
            let _ = self.cancellation.shutdown();
            self.thread
                .join()
                .map_err(|_| "worker sideband writer task failed".to_string())
        }
    }

    // Keep this blocking reader cancellable through retirement. A worker
    // descendant can retain the socket after writing only part of a frame, so
    // cancellation forwards complete buffered or immediately readable frames
    // with per-call nonblocking reads, then abandons any incomplete tail.
    struct SidebandReader {
        cancel: Cancellation,
        thread: thread::JoinHandle<()>,
    }

    impl SidebandReader {
        fn start(
            mut reader: crate::sideband::Reader,
            events: EventSender,
            failures: FailureReporter,
            controls: mpsc::Sender<Control>,
        ) -> Result<Self, (crate::sideband::Reader, String)> {
            let (cancelled, cancel) = match cancellation_pipe("worker sideband") {
                Ok(pipe) => pipe,
                Err(error) => return Err((reader, error)),
            };
            let thread = thread::spawn(move || {
                let mut ordinary_close = false;
                let mut sideband_failure = None;
                loop {
                    if let Err(error) = forward_buffered_sideband(&mut reader, &events) {
                        sideband_failure = Some(error);
                        break;
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
                        if let Err(error) = drain_retiring_sideband(&mut reader, &events) {
                            sideband_failure = Some(error);
                        }
                        break;
                    }
                    if !ready.stream {
                        continue;
                    }
                    let had_buffered_data = reader.has_buffered_data();
                    match reader.read_chunk() {
                        Ok(()) => {}
                        Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {}
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

    fn forward_buffered_sideband(
        reader: &mut crate::sideband::Reader,
        events: &EventSender,
    ) -> Result<(), String> {
        while let Some(message) = reader
            .receive_buffered::<WorkerMessage>()
            .map_err(|error| format!("worker sideband read failed: {error}"))?
        {
            events.send(message.into())?;
        }
        Ok(())
    }

    fn drain_retiring_sideband(
        reader: &mut crate::sideband::Reader,
        events: &EventSender,
    ) -> Result<(), String> {
        loop {
            forward_buffered_sideband(reader, events)?;
            match reader.read_chunk_nonblocking() {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::UnexpectedEof
                    ) =>
                {
                    return Ok(());
                }
                Err(error) => {
                    return Err(format!("worker sideband read failed: {error}"));
                }
            }
        }
    }

    fn discard_retiring_sideband(reader: &mut crate::sideband::Reader) -> Result<(), String> {
        loop {
            match reader.read_chunk_nonblocking() {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::UnexpectedEof
                    ) =>
                {
                    return Ok(());
                }
                Err(error) => {
                    return Err(format!("worker sideband read failed: {error}"));
                }
            }
        }
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
            stopping: Arc<AtomicBool>,
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
                                        if !stopping.load(Ordering::SeqCst) {
                                            failures.report(format!(
                                                "worker stdin write failed: {error}"
                                            ));
                                        }
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
                                        if !stopping.load(Ordering::SeqCst) {
                                            failures.report(
                                                "worker stdin write failed: write returned zero bytes"
                                                    .to_string(),
                                            );
                                        }
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
                                        if !stopping.load(Ordering::SeqCst) {
                                            failures.report(format!(
                                                "worker stdin write failed: {error}"
                                            ));
                                        }
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
        cancel: Cancellation,
        thread: thread::JoinHandle<()>,
    }

    #[derive(Clone, Copy)]
    enum OutputStream {
        Stdout,
        Stderr,
    }

    impl OutputReader {
        fn start<Stream>(
            mut stream: Stream,
            kind: OutputStream,
            events: EventSender,
            failures: FailureReporter,
        ) -> Result<Self, (Stream, String)>
        where
            Stream: Read + AsRawFd + Send + 'static,
        {
            if let Err(error) = set_nonblocking(&stream) {
                return Err((stream, error));
            }
            let (cancelled, cancel) = match cancellation_pipe("worker output") {
                Ok(pipe) => pipe,
                Err(error) => return Err((stream, error)),
            };
            let thread = thread::spawn(move || {
                let mut buffer = [0_u8; READ_CHUNK_SIZE];
                loop {
                    let ready = match wait_for_io(stream.as_raw_fd(), libc::POLLIN, &cancelled) {
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
                        if let Err(error) =
                            drain_buffered_output(&mut stream, kind, &events, &mut buffer)
                        {
                            failures.report(error);
                        }
                        break;
                    }
                }
            });
            Ok(Self { cancel, thread })
        }

        fn cancel_and_join(self) -> Result<(), String> {
            self.cancel.cancel();
            self.thread
                .join()
                .map_err(|_| "worker output reader task failed".to_string())
        }
    }

    fn drain_unstarted_output(
        mut stream: impl Read + AsRawFd,
        kind: OutputStream,
        events: &EventSender,
    ) -> Result<(), String> {
        let mut buffer = [0_u8; READ_CHUNK_SIZE];
        drain_buffered_output(&mut stream, kind, events, &mut buffer)
    }

    fn output_event(stream: OutputStream, bytes: &[u8]) -> RelayEvent {
        match (stream, std::str::from_utf8(bytes)) {
            (OutputStream::Stdout, Ok(data)) => RelayEvent::Stdout {
                data: data.to_string(),
            },
            (OutputStream::Stderr, Ok(data)) => RelayEvent::Stderr {
                data: data.to_string(),
            },
            (OutputStream::Stdout, Err(_)) => RelayEvent::StdoutBytes {
                data: EncodedBytes::from_bytes(bytes),
            },
            (OutputStream::Stderr, Err(_)) => RelayEvent::StderrBytes {
                data: EncodedBytes::from_bytes(bytes),
            },
        }
    }

    fn drain_buffered_output(
        stream: &mut (impl Read + AsRawFd),
        kind: OutputStream,
        events: &EventSender,
        buffer: &mut [u8],
    ) -> Result<(), String> {
        loop {
            match stream.read(buffer) {
                Ok(0) => break,
                Ok(length) => {
                    if events.send(output_event(kind, &buffer[..length])).is_err() {
                        break;
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(error) => return Err(format!("worker output read failed: {error}")),
            }
        }
        Ok(())
    }

    #[derive(Clone)]
    struct Cancellation(Arc<Mutex<Option<std::io::PipeWriter>>>);

    impl Cancellation {
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
