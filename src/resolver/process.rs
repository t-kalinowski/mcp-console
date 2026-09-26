use std::io::{self, PipeReader, PipeWriter, Write};
use std::mem::MaybeUninit;
use std::os::fd::AsRawFd;
use std::os::unix::process::CommandExt as _;
use std::path::Path;
use std::process::{Child, ChildStdin, Command, ExitStatus};
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::thread;

#[derive(Clone)]
pub(crate) struct ResolverStopHandle(Arc<dyn ResolverControl>);

pub(crate) trait ResolverControl: Send + Sync {
    fn stop(&self) -> Result<(), String>;
    fn interrupt(&self) -> Result<bool, String>;
    fn control_outcome(&self) -> Option<super::ResolverControlOutcome>;
    fn cleanup_confirmed(&self) -> bool;
}

impl ResolverStopHandle {
    pub(crate) fn new(control: impl ResolverControl + 'static) -> Self {
        Self(Arc::new(control))
    }
    pub(crate) fn stop(&self) -> Result<(), String> {
        self.0.stop()
    }
    pub(crate) fn interrupt(&self) -> Result<bool, String> {
        self.0.interrupt()
    }
    pub(crate) fn control_outcome(&self) -> Option<super::ResolverControlOutcome> {
        self.0.control_outcome()
    }
    pub(crate) fn cleanup_confirmed(&self) -> bool {
        self.0.cleanup_confirmed()
    }
}

struct LocalControl {
    events: Sender<ResolverEvent>,
    control: Arc<AtomicU8>,
    cleanup: Arc<AtomicBool>,
    waiting: Arc<Mutex<bool>>,
}

const CONTROL_NONE: u8 = 0;
const CONTROL_INTERRUPTED: u8 = 1;
const CONTROL_CANCELLED: u8 = 2;

enum ResolverEvent {
    Cancel,
    Interrupt {
        reply: Sender<Result<(), String>>,
        clear_marker: Option<Arc<AtomicU8>>,
    },
    Exited(io::Result<()>),
}

enum ResolverInterrupt {
    Signaled,
    AlreadyExited,
}

pub(crate) struct ResolverOutput {
    pub(crate) status: ExitStatus,
    pub(crate) write_result: io::Result<()>,
    pub(crate) stdout: Vec<u8>,
    pub(crate) stderr: Vec<u8>,
}

type OutputReceiver = Receiver<io::Result<Vec<u8>>>;

pub(crate) struct ResolverProcess {
    preparation: Option<crate::local_runtime::TemporaryDirectory>,
    events: Sender<ResolverEvent>,
    event_receiver: Receiver<ResolverEvent>,
    control: Arc<AtomicU8>,
    cleanup: Arc<AtomicBool>,
    waiting: Arc<Mutex<bool>>,
}

impl ResolverProcess {
    pub(crate) fn new() -> Self {
        let (events, event_receiver) = mpsc::channel();
        Self {
            preparation: None,
            events,
            event_receiver,
            control: Arc::new(AtomicU8::new(CONTROL_NONE)),
            cleanup: Arc::new(AtomicBool::new(false)),
            waiting: Arc::new(Mutex::new(false)),
        }
    }

    pub(crate) fn for_preparation(directory: Option<&Path>) -> Result<Self, String> {
        let mut process = Self::new();
        process.preparation = directory
            .map(crate::local_runtime::TemporaryDirectory::create_in)
            .transpose()?;
        Ok(process)
    }

    pub(crate) fn status_file(&self) -> Option<std::path::PathBuf> {
        self.preparation
            .as_ref()
            .map(|directory| directory.path().join("status"))
    }

    pub(crate) fn spawn(&self, command: &mut Command) -> io::Result<(Child, [OutputReceiver; 2])> {
        // Allocate exit notifications before spawning: setup failure must not
        // leave an unowned resolver. Native errors may leave inherited writers.
        let notifications = self
            .preparation
            .as_ref()
            .map(|_| Ok::<_, io::Error>([io::pipe()?, io::pipe()?]))
            .transpose()?;
        let (readers, writers) = match notifications {
            Some([(stdout, notify_stdout), (stderr, notify_stderr)]) => (
                [Some(stdout), Some(stderr)],
                vec![notify_stdout, notify_stderr],
            ),
            None => ([None, None], Vec::new()),
        };
        let mut child = command.spawn()?;
        let [stdout_exit, stderr_exit] = readers;
        let stdout = read_bounded_output(
            child.stdout.take().expect("piped resolver stdout"),
            stdout_exit,
        );
        let stderr = read_bounded_output(
            child.stderr.take().expect("piped resolver stderr"),
            stderr_exit,
        );
        self.watch_exit_with(child.id(), writers);
        Ok((child, [stdout, stderr]))
    }

    pub(crate) fn stop_handle(&self) -> ResolverStopHandle {
        ResolverStopHandle::new(LocalControl {
            events: self.events.clone(),
            control: self.control.clone(),
            cleanup: self.cleanup.clone(),
            waiting: self.waiting.clone(),
        })
    }

    // Mark the spawned child active before publishing its stop handle. An
    // interrupt in that gap must wait for the child's actual signal result.
    pub(crate) fn watch_exit(&self, pid: u32) {
        self.watch_exit_with(pid, Vec::new());
    }

    fn watch_exit_with(&self, pid: u32, notifications: Vec<PipeWriter>) {
        self.cleanup.store(false, Ordering::SeqCst);
        *self.waiting.lock().expect("resolver phase lock") = true;
        watch_resolver_exit(pid, self.events.clone(), notifications);
    }

    fn finish_wait(&self, kind: &str) -> Result<(), String> {
        let mut waiting = self.waiting.lock().expect("resolver phase lock");
        let mut cancelled = false;
        while let Ok(event) = self.event_receiver.try_recv() {
            match event {
                ResolverEvent::Interrupt { reply, .. } => {
                    let _ = reply.send(Ok(()));
                }
                ResolverEvent::Cancel => cancelled = true,
                ResolverEvent::Exited(_) => {}
            }
        }
        *waiting = false;
        if cancelled {
            Err(format!("{kind} resolution cancelled"))
        } else {
            Ok(())
        }
    }

    pub(crate) fn wait(
        &self,
        child: &mut Child,
        input: Receiver<io::Result<()>>,
        stdout: Receiver<io::Result<Vec<u8>>>,
        stderr: Receiver<io::Result<Vec<u8>>>,
        program: &Path,
        kind: &str,
    ) -> Result<ResolverOutput, String> {
        wait_for_resolver(self, child, input, stdout, stderr, program, kind)
    }

    pub(crate) fn abort(
        &self,
        child: &mut Child,
        program: &Path,
        kind: &str,
    ) -> Result<(), String> {
        let result = stop_resolver(child, program, kind, self.status_file().as_deref(), true);
        self.cleanup.store(result.is_ok(), Ordering::SeqCst);
        let _ = self.finish_wait(kind);
        result.map(|_| ())
    }
}

impl ResolverControl for LocalControl {
    fn stop(&self) -> Result<(), String> {
        let marked = self.mark_control(CONTROL_CANCELLED);
        if self.events.send(ResolverEvent::Cancel).is_err() {
            self.clear_control(CONTROL_CANCELLED, marked);
        }
        Ok(())
    }

    fn interrupt(&self) -> Result<bool, String> {
        let (reply, response) = mpsc::channel();
        let marked = self.mark_control(CONTROL_INTERRUPTED);
        let clear_marker = marked.then(|| self.control.clone());
        let waiting = self.waiting.lock().expect("resolver phase lock");
        let wait_for_reply = *waiting;
        if self
            .events
            .send(ResolverEvent::Interrupt {
                reply,
                clear_marker,
            })
            .is_err()
        {
            self.clear_control(CONTROL_INTERRUPTED, marked);
            return Ok(false);
        }
        drop(waiting);
        if !wait_for_reply {
            return Ok(true);
        }
        match response.recv() {
            Ok(result) => result.map(|()| true),
            // The resolver may finish after accepting the request but before
            // replying. The interrupt stays with that completed operation
            // rather than falling through to a different worker target.
            Err(_) => Ok(true),
        }
    }

    fn control_outcome(&self) -> Option<super::ResolverControlOutcome> {
        match self.control.load(Ordering::SeqCst) {
            CONTROL_INTERRUPTED => Some(super::ResolverControlOutcome::Interrupted),
            CONTROL_CANCELLED => Some(super::ResolverControlOutcome::Cancelled),
            CONTROL_NONE => None,
            _ => unreachable!("resolver control state is invalid"),
        }
    }

    fn cleanup_confirmed(&self) -> bool {
        self.cleanup.load(Ordering::SeqCst)
    }
}

impl LocalControl {
    fn mark_control(&self, control: u8) -> bool {
        self.control
            .compare_exchange(CONTROL_NONE, control, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
    }

    fn clear_control(&self, control: u8, marked: bool) {
        clear_control(self.control.as_ref(), control, marked);
    }
}

fn clear_control(state: &AtomicU8, control: u8, marked: bool) {
    if marked {
        let _ = state.compare_exchange(control, CONTROL_NONE, Ordering::SeqCst, Ordering::SeqCst);
    }
}

pub(crate) fn completed_write() -> Receiver<io::Result<()>> {
    let (sender, receiver) = mpsc::channel();
    sender
        .send(Ok(()))
        .expect("resolver completion receiver should be available");
    receiver
}

pub(crate) fn read_output(
    mut output: impl io::Read + Send + 'static,
) -> Receiver<io::Result<Vec<u8>>> {
    let (sender, receiver) = mpsc::channel();
    let _ = thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = output.read_to_end(&mut bytes).map(|_| bytes);
        let _ = sender.send(result);
    });
    receiver
}

fn read_bounded_output(
    output: impl io::Read + AsRawFd + Send + 'static,
    exited: Option<PipeReader>,
) -> Receiver<io::Result<Vec<u8>>> {
    match exited {
        Some(exited) => read_output(crate::process_output::RelayOutput::new(output, exited)),
        None => read_output(output),
    }
}

pub(super) fn write_input(mut input: ChildStdin, bytes: Vec<u8>) -> Receiver<io::Result<()>> {
    let (sender, receiver) = mpsc::channel();
    let _ = thread::spawn(move || {
        let _ = sender.send(input.write_all(&bytes));
    });
    receiver
}

pub(crate) fn resolver_command(program: &Path) -> Command {
    let mut command = Command::new(program);
    command.process_group(0);
    // SAFETY: the closure calls only libc signal functions after fork and
    // before exec. Resolver programs must not inherit an ignored or blocked
    // SIGINT from the MCP host.
    unsafe {
        command.pre_exec(|| {
            if libc::signal(libc::SIGINT, libc::SIG_DFL) == libc::SIG_ERR {
                return Err(io::Error::last_os_error());
            }
            let mut signals = std::mem::zeroed();
            if libc::sigemptyset(&mut signals) != 0
                || libc::sigaddset(&mut signals, libc::SIGINT) != 0
                || libc::sigprocmask(libc::SIG_UNBLOCK, &signals, std::ptr::null_mut()) != 0
            {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        });
    }
    command
}

fn watch_resolver_exit(pid: u32, events: Sender<ResolverEvent>, notifications: Vec<PipeWriter>) {
    let _ = thread::spawn(move || {
        let result = loop {
            let mut status = MaybeUninit::<libc::siginfo_t>::uninit();
            // SAFETY: `status` points to writable storage and `pid` identifies
            // the direct child. `WNOWAIT` leaves its status for `Child::wait`.
            let result = unsafe {
                libc::waitid(
                    libc::P_PID,
                    pid as libc::id_t,
                    status.as_mut_ptr(),
                    libc::WEXITED | libc::WNOWAIT,
                )
            };
            if result == 0 {
                break Ok(());
            }
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::Interrupted {
                break Err(error);
            }
        };
        drop(notifications);
        let _ = events.send(ResolverEvent::Exited(result));
    });
}

fn receive_result<T>(
    receiver: Receiver<io::Result<T>>,
    name: &str,
    kind: &str,
) -> Result<io::Result<T>, String> {
    receiver
        .recv()
        .map_err(|_| format!("{kind} resolver {name} task stopped"))
}

fn wait_for_resolver_exit(
    child: &mut Child,
    events: &Receiver<ResolverEvent>,
    program: &Path,
    kind: &str,
    cleanup: &AtomicBool,
    status_file: Option<&Path>,
) -> Result<ExitStatus, String> {
    let stop = |child: &mut Child, retiring| {
        let result = stop_resolver(child, program, kind, status_file, retiring);
        cleanup.store(result.is_ok(), Ordering::SeqCst);
        result
    };
    loop {
        match events.recv() {
            Ok(ResolverEvent::Cancel) => {
                stop(child, true)?;
                return Err(format!("{kind} resolution cancelled"));
            }
            Ok(ResolverEvent::Interrupt { reply, .. }) if status_file.is_some() => {
                // Managed preparation is disposable. Retire the whole native
                // launch rather than forwarding a cooperative signal through
                // uv and its subprocesses; keep the current worker untouched.
                let result = stop(child, true);
                let _ = reply.send(result.as_ref().map(|_| ()).map_err(Clone::clone));
                result?;
                return Err(format!("{kind} resolution interrupted"));
            }
            Ok(ResolverEvent::Interrupt {
                reply,
                clear_marker,
            }) => match interrupt_resolver(child) {
                Ok(ResolverInterrupt::Signaled) => {
                    let _ = reply.send(Ok(()));
                }
                Ok(ResolverInterrupt::AlreadyExited) => {
                    let _ = reply.send(Ok(()));
                }
                Err(error) => {
                    if let Some(control) = clear_marker {
                        clear_control(control.as_ref(), CONTROL_INTERRUPTED, true);
                    }
                    let message = format!(
                        "failed to interrupt {kind} resolver `{}`: {error}",
                        program.display()
                    );
                    let _ = reply.send(Err(message.clone()));
                    let _ = stop(child, true);
                    return Err(message);
                }
            },
            Ok(ResolverEvent::Exited(Ok(()))) => {
                return stop(child, false);
            }
            Ok(ResolverEvent::Exited(Err(error))) => {
                let _ = stop(child, true);
                return Err(format!(
                    "failed to wait for {kind} resolver `{}`: {error}",
                    program.display()
                ));
            }
            Err(_) => {
                let _ = stop(child, true);
                return Err(format!("{kind} resolver exit task stopped"));
            }
        }
    }
}

fn wait_for_resolver(
    resolver: &ResolverProcess,
    child: &mut Child,
    input: Receiver<io::Result<()>>,
    stdout: Receiver<io::Result<Vec<u8>>>,
    stderr: Receiver<io::Result<Vec<u8>>>,
    program: &Path,
    kind: &str,
) -> Result<ResolverOutput, String> {
    let status = wait_for_resolver_exit(
        child,
        &resolver.event_receiver,
        program,
        kind,
        &resolver.cleanup,
        resolver.status_file().as_deref(),
    );
    let phase_result = resolver.finish_wait(kind);
    let status = match status {
        Err(error) if resolver.preparation.is_some() => {
            let diagnostic = receive_result(stderr, "stderr reader", kind)?
                .map_err(|read_error| format!("{error}; {read_error}"))?;
            if diagnostic.is_empty() {
                return Err(error);
            }
            return Err(format!(
                "{error}: {}",
                String::from_utf8_lossy(&diagnostic).trim()
            ));
        }
        result => result?,
    };
    phase_result?;
    let write_result = receive_result(input, "stdin writer", kind)?;
    let stdout = receive_result(stdout, "stdout reader", kind)?
        .map_err(|error| format!("failed to read resolver stdout: {error}"))?;
    let stderr = receive_result(stderr, "stderr reader", kind)?
        .map_err(|error| format!("failed to read resolver stderr: {error}"))?;
    Ok(ResolverOutput {
        status,
        write_result,
        stdout,
        stderr,
    })
}

fn interrupt_resolver(child: &mut Child) -> io::Result<ResolverInterrupt> {
    let pid = child.id();
    // SAFETY: `process_group(0)` made the resolver PID its process-group ID.
    if unsafe { libc::killpg(pid as libc::pid_t, libc::SIGINT) } == 0 {
        return Ok(ResolverInterrupt::Signaled);
    }
    let error = io::Error::last_os_error();
    if matches!(error.raw_os_error(), Some(libc::EPERM) | Some(libc::ESRCH)) {
        // Keep an exited leader unreaped so its watcher remains authoritative
        // and this resolver PID cannot be reused before normal cleanup.
        if resolver_has_exited(pid)? {
            return Ok(ResolverInterrupt::AlreadyExited);
        }
    }
    Err(error)
}

fn resolver_has_exited(pid: u32) -> io::Result<bool> {
    let mut status = MaybeUninit::<libc::siginfo_t>::zeroed();
    // SAFETY: `status` points to zeroed writable storage. WNOWAIT observes the
    // direct child without reaping it, and WNOHANG makes a live child return.
    let result = unsafe {
        libc::waitid(
            libc::P_PID,
            pid as libc::id_t,
            status.as_mut_ptr(),
            libc::WEXITED | libc::WNOWAIT | libc::WNOHANG,
        )
    };
    if result != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: waitid initialized the zeroed structure before returning zero.
    Ok(unsafe { status.assume_init().si_pid() } == pid as libc::pid_t)
}

fn stop_resolver(
    child: &mut Child,
    program: &Path,
    kind: &str,
    status_file: Option<&Path>,
    retiring: bool,
) -> Result<ExitStatus, String> {
    if let Some(status_file) = status_file {
        // Let the existing native supervisor retire its descendants and storage.
        // Only successful runner exit confirms cleanup. A tiny shell wrapper
        // reports the resolver's exit separately, so package failures still have
        // a confirmed retirement and preserve the current worker transaction.
        if retiring {
            unsafe {
                libc::kill(child.id() as libc::pid_t, libc::SIGTERM);
            }
        }
        let status = child
            .wait()
            .map_err(|error| format!("cannot reap preparation runner: {error}"))?;
        if !status.success() {
            return Err(format!(
                "{kind} preparation runner failed ({status}); cleanup is unconfirmed"
            ));
        }
        if retiring {
            return Ok(status);
        }
        use std::os::unix::process::ExitStatusExt as _;
        let code = std::fs::read_to_string(status_file)
            .map_err(|error| format!("cannot read preparation status: {error}"))?
            .parse::<u8>()
            .map_err(|error| format!("invalid preparation status: {error}"))?;
        return Ok(ExitStatus::from_raw(i32::from(code) << 8));
    }

    // SAFETY: `process_group(0)` made the resolver PID its process-group ID.
    let result = unsafe { libc::killpg(child.id() as libc::pid_t, libc::SIGKILL) };
    if result < 0 {
        let kill_error = io::Error::last_os_error();
        return match child.try_wait() {
            // macOS reports EPERM when only the unreaped group leader remains.
            // ESRCH likewise means there is no remaining group to stop.
            Ok(Some(status))
                if matches!(
                    kill_error.raw_os_error(),
                    Some(libc::EPERM) | Some(libc::ESRCH)
                ) =>
            {
                Ok(status)
            }
            Ok(Some(_)) => Err(format!(
                "failed to stop {kind} resolver `{}`: {kill_error}",
                program.display()
            )),
            Ok(None) => Err(format!(
                "failed to stop {kind} resolver `{}`: {kill_error}",
                program.display()
            )),
            Err(wait_error) => Err(format!(
                "failed to stop {kind} resolver `{}`: {kill_error}; additionally failed to read its status: {wait_error}",
                program.display()
            )),
        };
    }
    child.wait().map_err(|error| {
        format!(
            "failed to reap {kind} resolver `{}`: {error}",
            program.display()
        )
    })
}
