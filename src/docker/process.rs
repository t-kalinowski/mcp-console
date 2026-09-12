//! Cancellable Docker CLI operations; diagnostics never enter protocol stdout.
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::target_launch::transfer::{Io, duplicate, poll};

#[derive(Clone)]
pub(super) struct Cancel {
    pub reader: Arc<io::PipeReader>,
    writer: Arc<Mutex<Option<io::PipeWriter>>>,
}

impl Cancel {
    pub fn new() -> Result<Self, String> {
        let (reader, writer) = io::pipe().map_err(|e| e.to_string())?;
        Ok(Self {
            reader: Arc::new(reader),
            writer: Arc::new(Mutex::new(Some(writer))),
        })
    }
    pub fn cancel(&self) {
        self.writer.lock().expect("cancellation lock").take();
    }
    pub fn check(&self) -> Result<(), String> {
        let mut event = libc::pollfd {
            fd: self.reader.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        if unsafe { libc::poll(&mut event, 1, 0) } < 0 {
            return Err(io::Error::last_os_error().to_string());
        }
        if event.revents != 0 {
            Err("Docker setup cancelled".into())
        } else {
            Ok(())
        }
    }
}

impl crate::resolver::ResolverControl for Cancel {
    fn stop(&self) -> Result<(), String> {
        self.cancel();
        Ok(())
    }
    fn interrupt(&self) -> Result<bool, String> {
        self.cancel();
        Ok(true)
    }
    fn control_outcome(&self) -> Option<crate::resolver::ResolverControlOutcome> {
        self.check()
            .err()
            .map(|_| crate::resolver::ResolverControlOutcome::Cancelled)
    }
    fn cleanup_confirmed(&self) -> bool {
        false
    }
}

pub(super) fn run(
    mut command: Command,
    cancel: &Cancel,
    deadline: Option<Instant>,
    diagnostics: bool,
    input: Option<Vec<u8>>,
) -> Result<Vec<u8>, String> {
    cancel.check()?;
    command
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(if diagnostics {
            Stdio::from(duplicate(2)?)
        } else {
            Stdio::piped()
        })
        .stderr(if diagnostics || input.is_some() {
            Stdio::inherit()
        } else {
            Stdio::piped()
        })
        .process_group(0);
    crate::process_descriptors::close_unlisted_from_multithreaded_parent(&mut command)?;
    let mut child = command
        .spawn()
        .map_err(|e| format!("cannot execute Docker command: {e}"))?;
    let (exited, notify) = io::pipe().map_err(|e| e.to_string())?;
    let mut exit =
        crate::process_exit::ChildExitWaiter::start_notifying(child.id(), move || drop(notify))?;
    let output = child.stdout.take().map(|stdout| {
        let exited = exited.try_clone().expect("exit pipe clone");
        std::thread::spawn(move || {
            let mut output = Vec::new();
            crate::process_output::RelayOutput::new(stdout, exited)
                .take((super::LIMIT + 1) as u64)
                .read_to_end(&mut output)
                .map_err(|e| e.to_string())?;
            if output.len() > super::LIMIT {
                return Err("Docker response exceeds 1 MiB".to_string());
            }
            Ok(output)
        })
    });
    let errors = child.stderr.take().map(|stderr| {
        let exited = exited.try_clone().expect("exit pipe clone");
        std::thread::spawn(move || {
            let mut errors = Vec::new();
            crate::process_output::RelayOutput::new(stderr, exited)
                .take((super::LIMIT + 1) as u64)
                .read_to_end(&mut errors)
                .map_err(|e| e.to_string())?;
            Ok::<_, String>(String::from_utf8_lossy(&errors).into_owned())
        })
    });
    let writer = input.map(|bytes| {
        let stdin = child.stdin.take().expect("piped setup input");
        let cancelled = cancel.reader.try_clone().expect("cancellation pipe clone");
        std::thread::spawn(move || -> Result<_, String> {
            let mut stdin = Io::new(stdin, Some(cancelled), deadline)?;
            stdin.write_all(&bytes).map_err(|e| e.to_string())?;
            // Keep the owner connected until it exits or setup is cancelled.
            Ok(stdin)
        })
    });
    let owner_input = writer
        .map(|task| task.join().map_err(|_| "Docker setup writer panicked")?)
        .transpose();
    let waited = poll(
        &[
            (exited.as_raw_fd(), libc::POLLIN),
            (cancel.reader.as_raw_fd(), libc::POLLIN),
        ],
        deadline,
    );
    let interrupted = match &waited {
        Ok(events) => events[1] != 0,
        Err(_) => true,
    };
    drop(owner_input);
    if interrupted {
        // Ownership helpers retire their container on input closure. Ordinary
        // image operations own no workload and can be killed immediately.
        if writer_is_owner(&command) {
            let _ = exit.wait(Duration::from_secs(8));
        }
        if !exit.wait(Duration::ZERO)? {
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
        }
    }
    if !exit.wait(Duration::from_secs(1))? {
        return Err("Docker CLI did not exit after cancellation".into());
    }
    let status = child.wait().map_err(|e| e.to_string())?;
    let output = output
        .map(|task| task.join().map_err(|_| "Docker output task panicked")?)
        .transpose()?;
    let errors = errors
        .map(|task| task.join().map_err(|_| "Docker diagnostic task panicked")?)
        .transpose()?
        .unwrap_or_default();
    waited?;
    if interrupted {
        return Err("Docker setup cancelled".into());
    }
    if !status.success() {
        return Err(format!("Docker command failed with {status}: {errors}"));
    }
    Ok(output.unwrap_or_default())
}

fn writer_is_owner(command: &Command) -> bool {
    command.get_args().any(|arg| arg == "docker-owner")
}
