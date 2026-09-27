use std::io::{self, Read, Write};
use std::mem::MaybeUninit;
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

pub(crate) struct ResolverProcess {
    native: bool,
    events: Sender<ResolverEvent>,
    event_receiver: Receiver<ResolverEvent>,
    control: Arc<AtomicU8>,
    cleanup: Arc<AtomicBool>,
    waiting: Arc<Mutex<bool>>,
}

type OutputReader = Receiver<io::Result<Vec<u8>>>;

impl ResolverProcess {
    pub(crate) fn new() -> Self {
        let (events, event_receiver) = mpsc::channel();
        Self {
            native: false,
            events,
            event_receiver,
            control: Arc::new(AtomicU8::new(CONTROL_NONE)),
            cleanup: Arc::new(AtomicBool::new(false)),
            waiting: Arc::new(Mutex::new(false)),
        }
    }

    pub(crate) fn native() -> Self {
        Self {
            native: true,
            ..Self::new()
        }
    }

    pub(crate) fn spawn(
        &self,
        command: &mut Command,
    ) -> io::Result<(Child, OutputReader, OutputReader)> {
        let (stdout_exit, stdout_done) = io::pipe()?;
        let (stderr_exit, stderr_done) = io::pipe()?;
        let mut child = command.spawn()?;
        let stdout = read_bounded_output(crate::process_output::RelayOutput::new(
            child.stdout.take().expect("resolver stdout"),
            stdout_exit,
        ));
        let stderr = read_diagnostics(crate::process_output::RelayOutput::new(
            child.stderr.take().expect("resolver stderr"),
            stderr_exit,
        ));
        self.cleanup.store(false, Ordering::SeqCst);
        *self.waiting.lock().expect("resolver phase lock") = true;
        watch_resolver_exit(
            child.id(),
            self.events.clone(),
            vec![stdout_done, stderr_done],
        );
        Ok((child, stdout, stderr))
    }

    pub(crate) fn stop_handle(&self) -> ResolverStopHandle {
        ResolverStopHandle::new(LocalControl {
            events: self.events.clone(),
            control: self.control.clone(),
            cleanup: self.cleanup.clone(),
            waiting: self.waiting.clone(),
        })
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
        let result = if self.native {
            retire_native(child, None)
        } else {
            stop_resolver(child, program, kind)
        };
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

fn read_bounded_output(
    mut output: impl io::Read + Send + 'static,
) -> Receiver<io::Result<Vec<u8>>> {
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        let result = (|| {
            let mut bytes = Vec::new();
            output
                .by_ref()
                .take((super::broker::LIMIT + 1) as u64)
                .read_to_end(&mut bytes)?;
            if bytes.len() > super::broker::LIMIT {
                io::copy(&mut output, &mut io::sink())?;
                return Err(io::Error::other("resolver output exceeds 1 MiB"));
            }
            Ok(bytes)
        })();
        let _ = sender.send(result);
    });
    receiver
}

fn read_diagnostics(output: impl io::Read + Send + 'static) -> Receiver<io::Result<Vec<u8>>> {
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        // Diagnostics are not protocol data. Retain both ends and exact UTF-8
        // omission accounting while leaving room for the operation's context.
        let result = crate::text_preview::TextPreview::read(output, 4096).map(String::into_bytes);
        let _ = sender.send(result);
    });
    receiver
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
    if !super::workload::active() {
        command.process_group(0);
    }
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

fn watch_resolver_exit(
    pid: u32,
    events: Sender<ResolverEvent>,
    notifications: Vec<io::PipeWriter>,
) {
    let _ = thread::spawn(move || {
        let result = crate::process_exit::wait_for_direct_child_exit(pid as libc::pid_t)
            .map_err(io::Error::other);
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
    native: bool,
) -> Result<ExitStatus, String> {
    let stop = |child: &mut Child| {
        let result = if native {
            retire_native(child, None)
        } else {
            stop_resolver(child, program, kind)
        };
        cleanup.store(result.is_ok(), Ordering::SeqCst);
        result
    };
    loop {
        match events.recv() {
            Ok(ResolverEvent::Cancel) => {
                stop(child)?;
                return Err(format!("{kind} resolution cancelled"));
            }
            Ok(ResolverEvent::Interrupt {
                reply,
                clear_marker,
            }) => {
                if native {
                    let result = retire_native(child, Some(reply));
                    cleanup.store(result.is_ok(), Ordering::SeqCst);
                    return result.and_then(|_| Err(format!("{kind} resolution interrupted")));
                }
                match interrupt_resolver(child) {
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
                        let _ = stop(child);
                        return Err(message);
                    }
                }
            }
            Ok(ResolverEvent::Exited(Ok(()))) => {
                if native {
                    let status = child.wait().map_err(|e| e.to_string())?;
                    cleanup.store(status.success(), Ordering::SeqCst);
                    if !status.success() {
                        return Err(format!(
                            "native resolver retirement is unconfirmed ({status})"
                        ));
                    }
                    return Ok(status);
                }
                return stop(child);
            }
            Ok(ResolverEvent::Exited(Err(error))) => {
                let _ = stop(child);
                return Err(format!(
                    "failed to wait for {kind} resolver `{}`: {error}",
                    program.display()
                ));
            }
            Err(_) => {
                let _ = stop(child);
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
        resolver.native,
    );
    let phase_result = resolver.finish_wait(kind);
    if resolver.native
        && let Err(error) = status
    {
        let stderr = receive_result(stderr, "stderr reader", kind)?.map_err(|e| e.to_string())?;
        let diagnostic = String::from_utf8_lossy(&stderr);
        return Err(if diagnostic.is_empty() {
            error
        } else {
            format!("{error}: {}", diagnostic.trim_end())
        });
    }
    let status = status?;
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

fn retire_native(
    child: &mut Child,
    acknowledged: Option<Sender<Result<(), String>>>,
) -> Result<ExitStatus, String> {
    let mut exit = crate::process_exit::ChildExitWaiter::start(child.id())?;
    // The runner owns descendants and turns retirement SIGTERM into exit 0.
    let signaled = unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM) };
    if let Some(reply) = acknowledged {
        let error = io::Error::last_os_error();
        let result = if signaled == 0 || error.raw_os_error() == Some(libc::ESRCH) {
            Ok(())
        } else {
            Err(format!("failed to interrupt native resolver: {error}"))
        };
        // Signal delivery is distinct from the later cleanup barrier.
        let _ = reply.send(result);
    }
    if !exit.wait(std::time::Duration::from_secs(6))? {
        let _ = child.kill();
        let _ = child.wait();
        return Err(
            "native resolver required forced termination; retirement is unconfirmed".into(),
        );
    }
    let status = child.wait().map_err(|e| e.to_string())?;
    if !status.success() {
        return Err(format!(
            "native resolver retirement is unconfirmed ({status})"
        ));
    }
    Ok(status)
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
        // A dying macOS child can disappear from process-group lookup before
        // waitid(WNOHANG) reports its exit. The unreaped direct child still owns
        // this PID; wait for its exit event instead of rejecting the interrupt.
        if unsafe { libc::getpgid(pid as libc::pid_t) } == -1
            && io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
        {
            crate::process_exit::wait_for_direct_child_exit(pid as libc::pid_t)
                .map_err(io::Error::other)?;
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

fn stop_resolver(child: &mut Child, program: &Path, kind: &str) -> Result<ExitStatus, String> {
    // Workload children share the enclosing lifetime's group. The outer native
    // runner (or the explicit direct-mode group) retires all remaining children.
    if super::workload::active() {
        if !resolver_has_exited(child.id()).map_err(|e| e.to_string())? {
            child.kill().map_err(|e| e.to_string())?;
        }
        return child.wait().map_err(|e| e.to_string());
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
