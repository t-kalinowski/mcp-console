use std::io::{self, Write};
use std::mem::MaybeUninit;
use std::path::Path;
use std::process::{Child, ChildStdin, ExitStatus};
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread;

#[derive(Clone)]
pub(crate) struct ResolverStopHandle(Sender<ResolverEvent>);

enum ResolverEvent {
    Cancel,
    Exited(io::Result<()>),
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
    match events.recv() {
        Ok(ResolverEvent::Cancel) => {
            stop_resolver(child, program, kind)?;
            Err(format!("{kind} resolution cancelled"))
        }
        Ok(ResolverEvent::Exited(Ok(()))) => stop_resolver(child, program, kind),
        Ok(ResolverEvent::Exited(Err(error))) => {
            let _ = stop_resolver(child, program, kind);
            Err(format!(
                "failed to wait for {kind} resolver `{}`: {error}",
                program.display()
            ))
        }
        Err(_) => {
            let _ = stop_resolver(child, program, kind);
            Err(format!("{kind} resolver exit task stopped"))
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
