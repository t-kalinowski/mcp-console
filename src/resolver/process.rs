use std::io::{self, Write};
use std::mem::MaybeUninit;
use std::os::unix::process::CommandExt as _;
use std::path::Path;
use std::process::{Child, ChildStdin, Command, ExitStatus};
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread;

#[derive(Clone)]
pub(crate) struct ResolverStopHandle(Sender<ResolverEvent>);

enum ResolverEvent {
    Cancel,
    Interrupt(Sender<Result<(), String>>),
    Exited(io::Result<()>),
}

enum ResolverInterrupt {
    Signaled,
    AlreadyExited,
}

pub(super) struct ResolverOutput {
    pub(super) status: ExitStatus,
    pub(super) write_result: io::Result<()>,
    pub(super) stdout: Vec<u8>,
    pub(super) stderr: Vec<u8>,
}

pub(super) struct ResolverProcess {
    events: Sender<ResolverEvent>,
    event_receiver: Receiver<ResolverEvent>,
}

impl ResolverProcess {
    pub(super) fn new() -> Self {
        let (events, event_receiver) = mpsc::channel();
        Self {
            events,
            event_receiver,
        }
    }

    pub(super) fn stop_handle(&self) -> ResolverStopHandle {
        ResolverStopHandle(self.events.clone())
    }

    pub(super) fn watch_exit(&self, pid: u32) {
        watch_resolver_exit(pid, self.events.clone());
    }

    pub(super) fn wait(
        &self,
        child: &mut Child,
        input: Receiver<io::Result<()>>,
        stdout: Receiver<io::Result<Vec<u8>>>,
        stderr: Receiver<io::Result<Vec<u8>>>,
        program: &Path,
        kind: &str,
    ) -> Result<ResolverOutput, String> {
        wait_for_resolver(
            child,
            &self.event_receiver,
            input,
            stdout,
            stderr,
            program,
            kind,
        )
    }
}

impl ResolverStopHandle {
    pub(crate) fn stop(&self) -> Result<(), String> {
        let _ = self.0.send(ResolverEvent::Cancel);
        Ok(())
    }

    pub(crate) fn interrupt(&self) -> Result<bool, String> {
        let (reply, response) = mpsc::channel();
        if self.0.send(ResolverEvent::Interrupt(reply)).is_err() {
            return Ok(false);
        }
        match response.recv() {
            Ok(result) => result.map(|()| true),
            // The resolver may finish after accepting the request but before
            // replying. The interrupt stays with that completed operation
            // rather than falling through to a different worker target.
            Err(_) => Ok(true),
        }
    }
}

pub(super) fn completed_write() -> Receiver<io::Result<()>> {
    let (sender, receiver) = mpsc::channel();
    sender
        .send(Ok(()))
        .expect("resolver completion receiver should be available");
    receiver
}

pub(super) fn read_output(
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

pub(super) fn resolver_command(program: &Path) -> Command {
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

fn watch_resolver_exit(pid: u32, events: Sender<ResolverEvent>) {
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
) -> Result<ExitStatus, String> {
    loop {
        match events.recv() {
            Ok(ResolverEvent::Cancel) => {
                stop_resolver(child, program, kind)?;
                return Err(format!("{kind} resolution cancelled"));
            }
            Ok(ResolverEvent::Interrupt(reply)) => match interrupt_resolver(child) {
                Ok(ResolverInterrupt::Signaled) => {
                    let _ = reply.send(Ok(()));
                }
                Ok(ResolverInterrupt::AlreadyExited) => {
                    let _ = reply.send(Ok(()));
                }
                Err(error) => {
                    let message = format!(
                        "failed to interrupt {kind} resolver `{}`: {error}",
                        program.display()
                    );
                    let _ = reply.send(Err(message.clone()));
                    let _ = stop_resolver(child, program, kind);
                    return Err(message);
                }
            },
            Ok(ResolverEvent::Exited(Ok(()))) => {
                return stop_resolver(child, program, kind);
            }
            Ok(ResolverEvent::Exited(Err(error))) => {
                let _ = stop_resolver(child, program, kind);
                return Err(format!(
                    "failed to wait for {kind} resolver `{}`: {error}",
                    program.display()
                ));
            }
            Err(_) => {
                let _ = stop_resolver(child, program, kind);
                return Err(format!("{kind} resolver exit task stopped"));
            }
        }
    }
}

fn wait_for_resolver(
    child: &mut Child,
    events: &Receiver<ResolverEvent>,
    input: Receiver<io::Result<()>>,
    stdout: Receiver<io::Result<Vec<u8>>>,
    stderr: Receiver<io::Result<Vec<u8>>>,
    program: &Path,
    kind: &str,
) -> Result<ResolverOutput, String> {
    let status = wait_for_resolver_exit(child, events, program, kind)?;
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

pub(super) fn stop_resolver(
    child: &mut Child,
    program: &Path,
    kind: &str,
) -> Result<ExitStatus, String> {
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
