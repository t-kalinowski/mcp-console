#[cfg(windows)]
pub(super) use crate::windows::resolver::Child;
use std::io;
use std::io::Write;
#[cfg(unix)]
use std::os::unix::process::CommandExt as _;
use std::path::Path;
#[cfg(unix)]
pub(super) use std::process::Child;
use std::process::ChildStdin;
use std::process::{Command, ExitStatus};
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::thread;

use crate::process_exit::ChildExitWaiter;

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
    Exited(Result<(), String>),
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
    exit: Mutex<Option<ChildExitWaiter>>,
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
            exit: Mutex::new(None),
            events,
            event_receiver,
            control: Arc::new(AtomicU8::new(CONTROL_NONE)),
            cleanup: Arc::new(AtomicBool::new(false)),
            waiting: Arc::new(Mutex::new(false)),
        }
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
        self.cleanup.store(false, Ordering::SeqCst);
        *self.waiting.lock().expect("resolver phase lock") = true;
        let events = self.events.clone();
        match ChildExitWaiter::start_observing(pid, move |result| {
            let _ = events.send(ResolverEvent::Exited(result));
        }) {
            Ok(exit) => *self.exit.lock().expect("resolver observer lock") = Some(exit),
            Err(error) => {
                let _ = self.events.send(ResolverEvent::Exited(Err(error)));
            }
        }
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
        let result = self.stop(child, program, kind);
        let _ = self.finish_wait(kind);
        result.map(|_| ())
    }

    fn stop(&self, child: &mut Child, program: &Path, kind: &str) -> Result<ExitStatus, String> {
        let mut exit = self.exit.lock().expect("resolver observer lock").take();
        let result = stop_resolver(child, program, kind, exit.as_mut());
        self.cleanup.store(result.is_ok(), Ordering::SeqCst);
        result
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

pub(super) fn write_input(mut input: ChildStdin, bytes: Vec<u8>) -> Receiver<io::Result<()>> {
    let (sender, receiver) = mpsc::channel();
    let _ = thread::spawn(move || {
        let _ = sender.send(input.write_all(&bytes));
    });
    receiver
}

#[cfg(unix)]
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
    resolver: &ResolverProcess,
) -> Result<ExitStatus, String> {
    let stop = |child: &mut Child| resolver.stop(child, program, kind);
    loop {
        match events.recv() {
            Ok(ResolverEvent::Cancel) => {
                stop(child)?;
                return Err(format!("{kind} resolution cancelled"));
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
                    let _ = stop(child);
                    return Err(message);
                }
            },
            Ok(ResolverEvent::Exited(Ok(()))) => {
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
    let status = wait_for_resolver_exit(child, &resolver.event_receiver, program, kind, resolver);
    let phase_result = resolver.finish_wait(kind);
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

#[cfg(unix)]
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
        if crate::process_exit::direct_child_has_exited(pid)? {
            return Ok(ResolverInterrupt::AlreadyExited);
        }
    }
    Err(error)
}

#[cfg(unix)]
fn stop_resolver(
    child: &mut Child,
    program: &Path,
    kind: &str,
    exit: Option<&mut ChildExitWaiter>,
) -> Result<ExitStatus, String> {
    // SAFETY: `process_group(0)` made the resolver PID its process-group ID.
    let result = unsafe { libc::killpg(child.id() as libc::pid_t, libc::SIGKILL) };
    if result < 0 {
        let kill_error = io::Error::last_os_error();
        match crate::process_exit::direct_child_has_exited(child.id()) {
            // macOS reports EPERM when only the unreaped group leader remains.
            // ESRCH likewise means there is no remaining group to stop.
            Ok(true)
                if matches!(
                    kill_error.raw_os_error(),
                    Some(libc::EPERM) | Some(libc::ESRCH)
                ) => {}
            Ok(_) => {
                return Err(format!(
                    "failed to stop {kind} resolver `{}`: {kill_error}",
                    program.display()
                ));
            }
            Err(wait_error) => {
                return Err(format!(
                    "failed to stop {kind} resolver `{}`: {kill_error}; additionally failed to read its status: {wait_error}",
                    program.display()
                ));
            }
        }
    }
    settle_observation(exit);
    child.wait().map_err(|error| {
        format!(
            "failed to reap {kind} resolver `{}`: {error}",
            program.display()
        )
    })
}

fn settle_observation(exit: Option<&mut ChildExitWaiter>) {
    if let Some(exit) = exit {
        // Exit/error is delivered through ResolverEvent::Exited. Cancellation
        // still owns termination and cleanup; this barrier only settles the
        // native observation and its event before reaping releases identity.
        let _ = exit.finish();
    }
}

#[cfg(windows)]
pub(crate) fn resolver_command(program: &Path) -> Command {
    use std::os::windows::process::CommandExt;
    let mut command = Command::new(program);
    command.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
    command
}
#[cfg(windows)]
fn interrupt_resolver(child: &mut Child) -> io::Result<ResolverInterrupt> {
    if child.try_wait()?.is_some() {
        return Ok(ResolverInterrupt::AlreadyExited);
    }
    child.terminate()?;
    Ok(ResolverInterrupt::Signaled)
}
#[cfg(windows)]
fn stop_resolver(
    child: &mut Child,
    program: &Path,
    kind: &str,
    exit: Option<&mut ChildExitWaiter>,
) -> Result<ExitStatus, String> {
    child.terminate().map_err(|error| {
        format!(
            "failed to retire {kind} resolver `{}`: {error}",
            program.display()
        )
    })?;
    settle_observation(exit);
    child.retire().map_err(|error| {
        format!(
            "failed to retire {kind} resolver `{}`: {error}",
            program.display()
        )
    })
}

#[cfg(unix)]
pub(crate) fn spawn_resolver(command: &mut Command) -> io::Result<Child> {
    command.spawn()
}
#[cfg(windows)]
pub(crate) fn spawn_resolver(command: &mut Command) -> io::Result<Child> {
    crate::windows::resolver::spawn(command)
}
