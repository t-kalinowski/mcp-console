//! Short-lived runtime discovery children share the worker's cancellation wait.

use std::io::{self, Read};
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::process::{Child, Command, Output, Stdio};
use std::time::Duration;

use super::{core, interrupt};
use crate::process_exit::ChildExitWaiter;
use crate::process_output::RelayOutput;

struct DiscoveryChild(Child);

impl Drop for DiscoveryChild {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

pub(crate) fn output(command: &mut Command) -> Result<Output, String> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    crate::process_descriptors::close_unlisted_from_multithreaded_parent(command)?;
    let mut child = DiscoveryChild(command.spawn().map_err(|error| error.to_string())?);
    let (exited, notify) = io::pipe().map_err(|error| error.to_string())?;
    let mut observer = ChildExitWaiter::start_notifying(child.0.id(), move || drop(notify))?;
    let stdout = RelayOutput::new(
        child.0.stdout.take().unwrap(),
        exited.try_clone().map_err(|e| e.to_string())?,
    );
    let stderr = RelayOutput::new(
        child.0.stderr.take().unwrap(),
        exited.try_clone().map_err(|e| e.to_string())?,
    );
    let (input_completion, input_finished) =
        UnixStream::pair().map_err(|error| error.to_string())?;
    let input_watch = crate::input_watch::InputWatch::new(input_completion.as_raw_fd())?;
    std::thread::scope(|scope| {
        scope.spawn(move || {
            if input_watch.wait(input_completion).is_err() {
                core::mark_shutting_down();
                interrupt::wake();
            }
        });
        let read = |mut stream: Box<dyn Read + Send>| {
            let mut bytes = Vec::new();
            stream
                .read_to_end(&mut bytes)
                .map_err(|error| error.to_string())?;
            Ok::<_, String>(bytes)
        };
        let stdout = scope.spawn(move || read(Box::new(stdout)));
        let stderr = scope.spawn(move || read(Box::new(stderr)));
        let result = wait(&mut observer, &exited);
        drop(input_finished);
        if result.is_err() {
            let _ = child.0.kill();
        }
        // Reap only after the observer has seen exit. Its notification also
        // releases output readers if a descendant retained either pipe.
        let observed = observer.wait(Duration::MAX);
        let status = child.0.wait().map_err(|error| error.to_string());
        let stdout = stdout.join().expect("discovery stdout reader");
        let stderr = stderr.join().expect("discovery stderr reader");
        result?;
        observed?;
        Ok(Output {
            status: status?,
            stdout: stdout?,
            stderr: stderr?,
        })
    })
}

fn wait(observer: &mut ChildExitWaiter, exited: &io::PipeReader) -> Result<(), String> {
    loop {
        if interrupt::pending() {
            return Err("KeyboardInterrupt".into());
        }
        core::observe_stdin_shutdown()?;
        if core::is_shutting_down() {
            return Err("worker is shutting down".into());
        }
        if observer.wait(Duration::ZERO)? {
            return Ok(());
        }
        let mut descriptors = [libc::pollfd {
            fd: exited.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        }];
        if interrupt::wait(&mut descriptors)? < 0
            && io::Error::last_os_error().kind() != io::ErrorKind::Interrupted
        {
            return Err(io::Error::last_os_error().to_string());
        }
    }
}
