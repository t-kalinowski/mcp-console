use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use super::io::{Cancellation, cancellation_pipe, set_nonblocking};
use super::supervisor::{Control, FailureReporter};
use crate::jsonl::JsonlBuffer;
use crate::readiness::wait_for_io;
use crate::relay_protocol::RelayCommand;
use crate::worker_protocol::ServerMessage;

const READ_CHUNK_SIZE: usize = 8 * 1024;
pub(super) const WORKER_SHUTDOWN_GRACE: Duration = Duration::from_secs(1);

pub(super) struct CommandReader {
    cancel: Cancellation,
    thread: thread::JoinHandle<()>,
}

impl CommandReader {
    pub(super) fn start(
        sideband: mpsc::Sender<SidebandWrite>,
        stdin: mpsc::Sender<StdinWrite>,
        controls: mpsc::Sender<Control>,
        failures: FailureReporter,
    ) -> Result<Self, String> {
        let (cancelled, cancel) = cancellation_pipe("relay stdin")?;
        let thread = thread::spawn(move || {
            let mut input = std::io::stdin();
            let mut buffer = JsonlBuffer::default();
            loop {
                let ready = match wait_for_io(input.as_raw_fd(), libc::POLLIN, Some(&cancelled)) {
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
                    Ok(0) if !buffer.has_buffered_data() => {
                        let _ = controls.send(Control::Shutdown {
                            deadline: Instant::now() + WORKER_SHUTDOWN_GRACE,
                            report_acceptance: false,
                        });
                        return;
                    }
                    Ok(0) => {
                        failures.report("relay stdin closed midway through a frame".to_string());
                        return;
                    }
                    Ok(length) => buffer.append(&chunk[..length]),
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(error) => {
                        failures.report(format!("relay stdin read failed: {error}"));
                        return;
                    }
                }
                loop {
                    let command = match buffer.next::<RelayCommand>() {
                        Ok(Some(command)) => command,
                        Ok(None) => break,
                        Err(error) => {
                            failures.report(format!("relay stdin frame is invalid: {error}"));
                            return;
                        }
                    };
                    let message = match command {
                        RelayCommand::Evaluate { language, source } => {
                            ServerMessage::Evaluate { language, source }
                        }
                        RelayCommand::PrepareR { library } => ServerMessage::PrepareR { library },
                        RelayCommand::RResolved { library } => ServerMessage::RResolved { library },
                        RelayCommand::RResolutionFailed { failure, message } => {
                            ServerMessage::RResolutionFailed { failure, message }
                        }
                        RelayCommand::PreparePython { packages } => {
                            ServerMessage::PreparePython { packages }
                        }
                        RelayCommand::PythonResolved { python, native } => {
                            ServerMessage::PythonResolved { python, native }
                        }
                        RelayCommand::PythonResolutionFailed { message } => {
                            ServerMessage::PythonResolutionFailed { message }
                        }
                        RelayCommand::PythonVersionResolved { version } => {
                            ServerMessage::PythonVersionResolved { version }
                        }
                        RelayCommand::PythonVersionResolutionFailed { message } => {
                            ServerMessage::PythonVersionResolutionFailed { message }
                        }
                        RelayCommand::Stdin { data } => {
                            if stdin.send(StdinWrite::Write(data.into_bytes())).is_err() {
                                failures.report("worker stdin writer stopped".to_string());
                                return;
                            }
                            continue;
                        }
                        RelayCommand::Interrupt { request_id } => {
                            if controls.send(Control::Interrupt { request_id }).is_err() {
                                failures.report("relay supervisor stopped".to_string());
                                return;
                            }
                            continue;
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
                    };
                    if sideband.send(SidebandWrite::Message(message)).is_err() {
                        failures.report("worker sideband writer stopped".to_string());
                        return;
                    }
                }
            }
        });
        Ok(Self { cancel, thread })
    }

    pub(super) fn cancel_and_join(self) -> Result<(), String> {
        self.cancel.cancel();
        self.thread
            .join()
            .map_err(|_| "relay stdin reader task failed".to_string())
    }
}

pub(super) struct SidebandWriter {
    sender: mpsc::Sender<SidebandWrite>,
    cancel: Cancellation,
    thread: thread::JoinHandle<()>,
}

pub(super) enum SidebandWrite {
    Message(ServerMessage),
    Close,
}

impl SidebandWriter {
    pub(super) fn start(
        writer: crate::sideband::Writer,
        failures: FailureReporter,
        stopping: Arc<AtomicBool>,
    ) -> Result<Self, String> {
        let (sender, receiver) = mpsc::channel();
        let (cancelled, cancel) = cancellation_pipe("worker sideband writer")?;
        let thread = thread::spawn(move || {
            for message in receiver {
                match message {
                    SidebandWrite::Message(message) => {
                        if let Err(error) = writer.send_cancellable(&message, Some(&cancelled)) {
                            if !stopping.load(Ordering::SeqCst) {
                                failures.report(format!("worker sideband write failed: {error}"));
                            }
                            return;
                        }
                    }
                    SidebandWrite::Close => return,
                }
            }
        });
        Ok(Self {
            sender,
            cancel,
            thread,
        })
    }

    pub(super) fn sender(&self) -> mpsc::Sender<SidebandWrite> {
        self.sender.clone()
    }

    pub(super) fn cancel_and_join(self) -> Result<(), String> {
        let _ = self.sender.send(SidebandWrite::Close);
        self.cancel.cancel();
        self.thread
            .join()
            .map_err(|_| "worker sideband writer task failed".to_string())
    }
}

pub(super) struct StdinWriter {
    sender: mpsc::Sender<StdinWrite>,
    cancel: Cancellation,
    thread: thread::JoinHandle<()>,
}

pub(super) enum StdinWrite {
    Write(Vec<u8>),
    Close,
}

impl StdinWriter {
    pub(super) fn start(
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
                                Some(&cancelled),
                            ) {
                                Ok(ready) => ready,
                                Err(error) => {
                                    if !stopping.load(Ordering::SeqCst) {
                                        failures
                                            .report(format!("worker stdin write failed: {error}"));
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
                                        failures
                                            .report(format!("worker stdin write failed: {error}"));
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

    pub(super) fn sender(&self) -> mpsc::Sender<StdinWrite> {
        self.sender.clone()
    }

    pub(super) fn cancel_and_join(self) -> Result<(), String> {
        let _ = self.sender.send(StdinWrite::Close);
        self.cancel.cancel();
        self.thread
            .join()
            .map_err(|_| "worker stdin writer task failed".to_string())
    }
}
