//! Target CLI operations retain control attribution independently of I/O wakes.
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::process::CommandExt;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::resolver::ResolverControlOutcome;
use crate::target_launch::SetupFailure;
use crate::target_launch::transfer::{duplicate, poll};

#[derive(Clone)]
pub(crate) struct Cancel {
    pub protocol: crate::target_launch::Protocol,
    pub reader: Arc<io::PipeReader>,
    state: Arc<Mutex<Control>>,
    diagnostics: Option<crate::process_output::Diagnostics>,
}

struct Control {
    writer: Option<io::PipeWriter>,
    cause: Option<ResolverControlOutcome>,
    local_retired: bool,
    report: Option<SetupReport>,
}

/// Evidence for the complete capture/probe operation, not a generation's
/// resources. Generation and provider owners remain their retirement authority.
struct SetupReport {
    result: Result<(), SetupFailure>,
    local_retired: bool,
    provider_confirmed: bool,
}

impl Cancel {
    pub fn new(protocol: crate::target_launch::Protocol) -> Result<Self, String> {
        let (reader, writer) = io::pipe().map_err(|e| e.to_string())?;
        Ok(Self {
            protocol,
            reader: Arc::new(reader),
            state: Arc::new(Mutex::new(Control {
                writer: Some(writer),
                cause: None,
                local_retired: true,
                report: None,
            })),
            diagnostics: None,
        })
    }
    pub fn with_diagnostics(mut self, output: crate::process_output::Diagnostics) -> Self {
        self.diagnostics = Some(output);
        self
    }
    /// Wake blocked I/O, including internal transfer teardown. This is never
    /// evidence of a requested control or of resource retirement.
    pub fn cancel(&self) {
        self.state
            .lock()
            .expect("target control lock")
            .writer
            .take();
    }
    fn request(&self, cause: ResolverControlOutcome) -> bool {
        let mut state = self.state.lock().expect("target control lock");
        if state.report.is_some() {
            return false;
        }
        state.cause.get_or_insert(cause);
        state.writer.take();
        true
    }
    fn cause(&self) -> Option<ResolverControlOutcome> {
        self.state.lock().expect("target control lock").cause
    }
    pub fn check(&self) -> Result<(), String> {
        self.check_setup()
            .map_err(|error| error.message(self.protocol, self.cause()))
    }
    fn check_setup(&self) -> Result<(), SetupFailure> {
        let mut event = libc::pollfd {
            fd: self.reader.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        if unsafe { libc::poll(&mut event, 1, 0) } < 0 {
            return Err(io::Error::last_os_error().to_string().into());
        }
        if self.cause().is_some() {
            Err(SetupFailure::controlled())
        } else if event.revents != 0 {
            Err("target setup I/O aborted".into())
        } else {
            Ok(())
        }
    }
    /// Completion and control acceptance share one lock. An accepted cause
    /// survives retirement, while later controls cannot target the next stage.
    pub fn finish<T>(
        &self,
        mut result: Result<T, SetupFailure>,
        provider_confirmed: bool,
    ) -> Result<T, String> {
        let mut state = self.state.lock().expect("target control lock");
        if result.is_ok() && state.cause.is_some() {
            result = Err(SetupFailure::controlled());
        }
        state.report = Some(SetupReport {
            result: result.as_ref().map(|_| ()).map_err(Clone::clone),
            local_retired: state.local_retired,
            provider_confirmed,
        });
        result.map_err(|error| error.message(self.protocol, state.cause))
    }
}

impl crate::resolver::ResolverControl for Cancel {
    fn stop(&self) -> Result<(), String> {
        self.request(ResolverControlOutcome::Cancelled);
        Ok(())
    }
    fn interrupt(&self) -> Result<bool, String> {
        Ok(self.request(ResolverControlOutcome::Interrupted))
    }
    fn control_outcome(&self) -> Option<ResolverControlOutcome> {
        self.cause()
    }
    fn cleanup_confirmed(&self) -> bool {
        self.state
            .lock()
            .expect("target control lock")
            .report
            .as_ref()
            .is_some_and(|report| report.local_retired && report.provider_confirmed)
    }
    fn failure_is_controlled(&self) -> bool {
        self.state
            .lock()
            .expect("target control lock")
            .report
            .as_ref()
            .is_some_and(|report| {
                report
                    .result
                    .as_ref()
                    .err()
                    .is_some_and(|failure| failure.error.is_none())
            })
    }
}

pub(crate) struct OwnerInput {
    pub bytes: Vec<u8>,
    pub retirement_grace: Duration,
}

#[derive(Clone, Copy)]
pub(crate) enum OutputMode {
    /// Capture provider data and retain stderr for a failed command's error.
    Capture,
    /// Stream setup output and errors to the diagnostic owner.
    Diagnostics,
    /// Capture stdout data while streaming provider diagnostics on stderr.
    Data,
}

/// A command's operation result and retained protocol output stay available
/// after its local child, observer, and all I/O tasks have settled.
pub(crate) struct CommandReport {
    pub output: Vec<u8>,
    pub result: Result<(), SetupFailure>,
    pub status: Option<std::process::ExitStatus>,
}

/// An I/O failure retires only this command. The operation's control token
/// remains available for subsequent provider cleanup commands.
#[derive(Clone)]
struct CommandAbort(Arc<Mutex<Option<io::PipeWriter>>>);

impl CommandAbort {
    fn abort(&self) {
        self.0.lock().expect("target command abort lock").take();
    }
}

pub(crate) fn run_setup(
    command: Command,
    cancel: &Cancel,
    deadline: Option<Instant>,
    mode: OutputMode,
    input: Option<OwnerInput>,
) -> Result<Vec<u8>, SetupFailure> {
    let report = run_report(command, cancel, deadline, mode, input)?;
    report.result?;
    Ok(report.output)
}

/// Provider-owner callers retain their existing string diagnostic boundary.
pub(crate) fn run(
    command: Command,
    cancel: &Cancel,
    deadline: Option<Instant>,
    mode: OutputMode,
    input: Option<OwnerInput>,
) -> Result<Vec<u8>, String> {
    run_setup(command, cancel, deadline, mode, input)
        .map_err(|error| error.message(cancel.protocol, cancel.cause()))
}

fn write_input(
    mut stdin: ChildStdin,
    bytes: &[u8],
    cancel: &io::PipeReader,
    aborted: &io::PipeReader,
    deadline: Option<Instant>,
) -> Result<Option<ChildStdin>, String> {
    let flags = unsafe { libc::fcntl(stdin.as_raw_fd(), libc::F_GETFL) };
    if flags < 0
        || unsafe { libc::fcntl(stdin.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0
    {
        return Err(io::Error::last_os_error().to_string());
    }
    let mut remaining = bytes;
    while !remaining.is_empty() {
        let events = poll(
            &[
                (stdin.as_raw_fd(), libc::POLLOUT),
                (cancel.as_raw_fd(), libc::POLLIN),
                (aborted.as_raw_fd(), libc::POLLIN),
            ],
            deadline,
        )?;
        if events[1] != 0 || events[2] != 0 {
            return Ok(None); // I/O abort; logical cause remains with the owner.
        }
        match stdin.write(remaining) {
            Ok(0) => return Err("failed to write target setup input: write zero".into()),
            Ok(count) => remaining = &remaining[count..],
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) => {}
            Err(error) => return Err(format!("failed to write target setup input: {error}")),
        }
    }
    Ok(Some(stdin))
}

fn collect<R: Read + AsRawFd>(
    source: R,
    stopped: io::PipeReader,
    abort: CommandAbort,
    label: &'static str,
) -> (Vec<u8>, io::Result<()>) {
    let mut bytes = Vec::new();
    let result = crate::process_output::RelayOutput::new(source, stopped)
        .take((crate::target_launch::MAX_BOOTSTRAP + 1) as u64)
        .read_to_end(&mut bytes)
        .and_then(|_| {
            if bytes.len() > crate::target_launch::MAX_BOOTSTRAP {
                Err(io::Error::other(format!("{label} response exceeds 1 MiB")))
            } else {
                Ok(())
            }
        });
    if result.is_err() {
        abort.abort();
    }
    (bytes, result)
}

pub(crate) fn run_report(
    mut command: Command,
    cancel: &Cancel,
    deadline: Option<Instant>,
    mode: OutputMode,
    input: Option<OwnerInput>,
) -> Result<CommandReport, SetupFailure> {
    cancel.check_setup()?;
    let label = cancel.protocol.0;
    let owner = input.is_some();
    let retirement_grace = input.as_ref().map(|input| input.retirement_grace);
    command
        .stdin(if owner { Stdio::piped() } else { Stdio::null() })
        .stdout(if matches!(mode, OutputMode::Diagnostics) {
            Stdio::from(duplicate(2)?)
        } else {
            Stdio::piped()
        })
        .stderr(if !matches!(mode, OutputMode::Capture) || owner {
            Stdio::inherit()
        } else {
            Stdio::piped()
        })
        .process_group(0);
    let diagnostics = if let Some(output) = &cancel.diagnostics
        && (!matches!(mode, OutputMode::Capture) || owner)
    {
        let (reader, writer) = io::pipe().map_err(|error| error.to_string())?;
        command.stderr(Stdio::from(
            writer.try_clone().map_err(|error| error.to_string())?,
        ));
        if matches!(mode, OutputMode::Diagnostics) {
            command.stdout(Stdio::from(writer));
        }
        Some((reader, output.clone()))
    } else {
        None
    };
    // Prepare fallible pipe setup before spawning. Output stop is independent
    // of exit observation, so failed retirement can still join every reader.
    let (exited, notify) = io::pipe().map_err(|error| error.to_string())?;
    let (stopped, stop_output) = io::pipe().map_err(|error| error.to_string())?;
    let stdout_stop = stopped.try_clone().map_err(|error| error.to_string())?;
    let stderr_stop = stopped.try_clone().map_err(|error| error.to_string())?;
    let (io_aborted, abort) = io::pipe().map_err(|error| error.to_string())?;
    let abort = CommandAbort(Arc::new(Mutex::new(Some(abort))));
    let input_abort = io_aborted.try_clone().map_err(|error| error.to_string())?;
    crate::process_descriptors::close_unlisted_from_multithreaded_parent(&mut command)?;
    let mut child = {
        // Command admission and control acceptance share the setup lock. Once
        // accepted, a control cannot slip between a check and the next spawn.
        let state = cancel.state.lock().expect("target control lock");
        if state.cause.is_some() {
            return Err(SetupFailure::controlled());
        }
        if state.writer.is_none() {
            return Err("target setup I/O aborted".into());
        }
        command
            .spawn()
            .map_err(|error| format!("cannot execute {label} command: {error}"))?
    };
    drop(command);
    let observation =
        crate::process_exit::ChildExitWaiter::start_cancellable(child.id(), move |_| drop(notify));
    let mut errors = Vec::new();
    let mut exit = match observation {
        Ok(exit) => Some(exit),
        Err(error) => {
            errors.push(error);
            None
        }
    };
    let diagnostic_task = diagnostics.map(|(reader, output)| {
        let abort = abort.clone();
        std::thread::spawn(move || {
            let result = crate::process_output::forward(reader, stopped, output);
            if result.is_err() {
                abort.abort();
            }
            result
        })
    });
    let output = child.stdout.take().map(|stdout| {
        let abort = abort.clone();
        std::thread::spawn(move || collect(stdout, stdout_stop, abort, label))
    });
    let stderr = child.stderr.take().map(|stderr| {
        let abort = abort.clone();
        std::thread::spawn(move || collect(stderr, stderr_stop, abort, label))
    });
    let writer = input.map(|input| {
        let stdin = child.stdin.take().expect("piped setup input");
        let cancel = cancel.clone();
        let abort = abort.clone();
        std::thread::spawn(move || {
            let result = write_input(stdin, &input.bytes, &cancel.reader, &input_abort, deadline);
            if result.is_err() {
                abort.abort();
            }
            result
        })
    });
    let mut tasks_joined = true;
    let owner_input = match writer.map(|task| task.join()) {
        Some(Ok(Ok(stdin))) => stdin,
        Some(Ok(Err(error))) => {
            errors.push(error);
            None
        }
        Some(Err(_)) => {
            errors.push("target setup writer panicked".into());
            tasks_joined = false;
            None
        }
        None => None,
    };
    let mut aborted = !errors.is_empty();
    if !aborted {
        match poll(
            &[
                (exited.as_raw_fd(), libc::POLLIN),
                (cancel.reader.as_raw_fd(), libc::POLLIN),
                (io_aborted.as_raw_fd(), libc::POLLIN),
            ],
            deadline,
        ) {
            Ok(events) => {
                aborted = events[1] != 0 || events[2] != 0;
                if events[0] != 0
                    && let Some(exit) = exit.as_mut()
                    && let Err(error) = exit.wait(Duration::ZERO)
                {
                    errors.push(error);
                    aborted = true;
                }
            }
            Err(error) => {
                errors.push(error);
                aborted = true;
            }
        }
    }
    drop(owner_input);
    let process = retire(
        &mut child,
        exit.as_mut(),
        if aborted { retirement_grace } else { None },
        aborted,
    );
    let process_retired = process.is_ok();
    if let Err(error) = &process {
        errors.push(error.clone());
    }
    let observation = exit.as_mut().map_or(
        Ok(None),
        crate::process_exit::ChildExitWaiter::cancel_and_finish,
    );
    let observation_settled = observation.is_ok();
    match observation {
        Ok(Some(error)) | Err(error) => errors.push(error),
        Ok(None) => {}
    }
    drop(stop_output);
    let mut bytes = Vec::new();
    let mut diagnostic_bytes = Vec::new();
    for (name, task, destination) in [
        ("output", output, &mut bytes),
        ("diagnostic", stderr, &mut diagnostic_bytes),
    ] {
        match task.map(|task| task.join()) {
            Some(Ok((captured, result))) => {
                *destination = captured;
                if let Err(error) = result {
                    errors.push(format!("{label} {name} read failed: {error}"));
                }
            }
            Some(Err(_)) => {
                errors.push(format!("target {name} task panicked"));
                tasks_joined = false;
            }
            None => {}
        }
    }
    match diagnostic_task.map(|task| task.join()) {
        Some(Ok(Err(error))) => errors.push(format!("{label} diagnostic read failed: {error}")),
        Some(Err(_)) => {
            errors.push("target diagnostic task panicked".into());
            tasks_joined = false;
        }
        _ => {}
    }
    cancel
        .state
        .lock()
        .expect("target control lock")
        .local_retired &= process_retired && observation_settled && tasks_joined;
    if !process_retired {
        // Preserve ownership without claiming retirement. Reap only after a
        // future non-reaping exit observation; replacement remains forbidden.
        std::thread::spawn(move || {
            if crate::process_exit::wait_for_direct_child_exit(child.id() as libc::pid_t).is_ok() {
                let _ = child.wait();
            }
        });
    }
    let status = process.ok();
    if let Some(status) = status
        && !status.success()
        && !owner
        && (!aborted || status.code().is_some())
    {
        diagnostic_bytes.extend_from_slice(&bytes);
        let diagnostic = String::from_utf8_lossy(&diagnostic_bytes);
        errors.push(if diagnostic.is_empty() {
            format!("{label} command failed with {status}")
        } else {
            format!("{label} command failed with {status}: {diagnostic}")
        });
    }
    let result = if !errors.is_empty() {
        Err(errors.join("; ").into())
    } else if cancel.cause().is_some() {
        Err(SetupFailure::controlled())
    } else {
        Ok(())
    };
    Ok(CommandReport {
        output: bytes,
        result,
        status,
    })
}

fn retire(
    child: &mut Child,
    mut exit: Option<&mut crate::process_exit::ChildExitWaiter>,
    grace: Option<Duration>,
    aborted: bool,
) -> Result<std::process::ExitStatus, String> {
    if aborted {
        if let (Some(grace), Some(exit)) = (grace, exit.as_mut()) {
            let _ = exit.wait(grace);
        }
        if !exit
            .as_mut()
            .is_some_and(|exit| exit.wait(Duration::ZERO) == Ok(true))
        {
            let result = unsafe { libc::kill(-(child.id() as i32), libc::SIGKILL) };
            if result < 0 {
                let error = io::Error::last_os_error();
                if !matches!(error.raw_os_error(), Some(libc::EPERM) | Some(libc::ESRCH))
                    || !crate::process_exit::direct_child_has_exited(child.id())
                        .map_err(|error| error.to_string())?
                {
                    return Err(format!("failed to stop target CLI: {error}"));
                }
            }
        }
    }
    let observed = match exit {
        Some(exit) => {
            let observed = exit.wait(Duration::from_secs(1))?;
            if observed {
                exit.finish()?;
            }
            observed
        }
        None => crate::process_exit::direct_child_has_exited(child.id())
            .map_err(|error| error.to_string())?,
    };
    if !observed {
        return Err("target CLI did not exit after retirement".into());
    }
    child.wait().map_err(|error| error.to_string())
}
