use std::fs::File;
use std::io::{self, Write};
use std::os::fd::{AsFd, AsRawFd};
use std::sync::{Arc, OnceLock, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use crate::relay_protocol::RelayEvent;

const RETIREMENT_FLUSH_TIMEOUT: Duration = Duration::from_secs(1);

#[derive(Clone)]
pub(super) struct EventSender(mpsc::Sender<EventRequest>);

enum EventRequest {
    Send(Box<RelayEvent>),
    Finish,
}

pub(super) struct EventWriter {
    deadline: Arc<OnceLock<Instant>>,
    wake: Option<io::PipeWriter>,
    thread: thread::JoinHandle<Result<(), String>>,
}

pub(super) fn start(
    on_error: impl FnOnce(String) + Send + 'static,
) -> Result<(EventSender, EventWriter), String> {
    let (reader, wake) =
        io::pipe().map_err(|error| format!("failed to create relay stdout wake pipe: {error}"))?;
    let deadline = Arc::new(OnceLock::new());
    let output = EventOutput::new(reader, deadline.clone())
        .map_err(|error| format!("failed to configure relay stdout: {error}"))?;
    let (sender, receiver) = mpsc::channel();
    let thread = thread::spawn(move || {
        let mut writer = output;
        for request in receiver {
            match request {
                EventRequest::Send(event) => {
                    // Give the deadline-aware writer a complete frame rather
                    // than making a syscall for each JSON punctuation fragment.
                    let mut frame = serde_json::to_vec(&event)
                        .expect("relay event serialization should succeed");
                    frame.push(b'\n');
                    drop(event);
                    if let Err(error) = writer.write_all(&frame) {
                        let error = format!("relay stdout write failed: {error}");
                        on_error(error.clone());
                        return Err(error);
                    }
                }
                EventRequest::Finish => return Ok(()),
            }
        }
        Ok(())
    });
    Ok((
        EventSender(sender),
        EventWriter {
            deadline,
            wake: Some(wake),
            thread,
        },
    ))
}

impl EventSender {
    pub(super) fn send(&self, event: RelayEvent) -> Result<(), String> {
        self.0
            .send(EventRequest::Send(Box::new(event)))
            .map_err(|_| "relay event writer stopped".to_string())
    }

    pub(super) fn finish(&self) -> Result<(), String> {
        self.0
            .send(EventRequest::Finish)
            .map_err(|_| "relay event writer stopped".to_string())
    }
}

impl EventWriter {
    pub(super) fn begin_retirement(&mut self) {
        self.deadline
            .set(Instant::now() + RETIREMENT_FLUSH_TIMEOUT)
            .expect("relay stdout should retire only once");
        // Wake a writer already waiting without a deadline. Every subsequent
        // write and wait shares the deadline, including during reader joins.
        drop(self.wake.take());
    }

    pub(super) fn join(self) -> Result<(), String> {
        self.thread
            .join()
            .map_err(|_| "relay event writer task failed".to_string())?
    }
}

struct EventOutput {
    file: File,
    original_flags: libc::c_int,
    wake: io::PipeReader,
    deadline: Arc<OnceLock<Instant>>,
}

impl EventOutput {
    fn new(wake: io::PipeReader, deadline: Arc<OnceLock<Instant>>) -> io::Result<Self> {
        let file = File::from(io::stdout().as_fd().try_clone_to_owned()?);
        // SAFETY: the owned duplicate remains open throughout configuration.
        let original_flags = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_GETFL) };
        if original_flags < 0 {
            return Err(io::Error::last_os_error());
        }
        let output = Self {
            file,
            original_flags,
            wake,
            deadline,
        };
        // Duplicates share file status flags. The relay is the sole protocol
        // writer; restore the original flags when its output task finishes.
        // SAFETY: this preserves existing flags on the live output descriptor.
        if unsafe {
            libc::fcntl(
                output.file.as_raw_fd(),
                libc::F_SETFL,
                original_flags | libc::O_NONBLOCK,
            )
        } < 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(output)
    }

    fn check_deadline(&self) -> io::Result<()> {
        if self
            .deadline
            .get()
            .is_some_and(|deadline| Instant::now() >= *deadline)
        {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "relay stdout retirement deadline expired",
            ));
        }
        Ok(())
    }

    fn wait_writable(&self) -> io::Result<()> {
        loop {
            self.check_deadline()?;
            let deadline = self.deadline.get();
            let timeout = deadline.map_or(-1, |deadline| {
                deadline
                    .saturating_duration_since(Instant::now())
                    .as_millis()
                    .max(1) as libc::c_int
            });
            let mut descriptors = [
                libc::pollfd {
                    fd: self.file.as_raw_fd(),
                    events: libc::POLLOUT,
                    revents: 0,
                },
                libc::pollfd {
                    // Once retirement starts, EOF has served its one wakeup.
                    // Polling it again would spin instead of waiting for output.
                    fd: if deadline.is_some() {
                        -1
                    } else {
                        self.wake.as_raw_fd()
                    },
                    events: libc::POLLIN,
                    revents: 0,
                },
            ];
            // SAFETY: the descriptors remain open and the array is initialized.
            if unsafe { libc::poll(descriptors.as_mut_ptr(), descriptors.len() as _, timeout) } >= 0
            {
                return Ok(());
            }
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
    }
}

impl Write for EventOutput {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        loop {
            self.check_deadline()?;
            match self.file.write(bytes) {
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => self.wait_writable()?,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
                result => return result,
            }
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        // File writes are unbuffered. Once write_all succeeds, no pending
        // bytes remain for a deadline to expire during this no-op.
        Ok(())
    }
}

impl Drop for EventOutput {
    fn drop(&mut self) {
        // SAFETY: the owned descriptor is still open during Drop.
        let _ = unsafe { libc::fcntl(self.file.as_raw_fd(), libc::F_SETFL, self.original_flags) };
    }
}
