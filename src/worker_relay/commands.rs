use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use super::io::{Cancellation, cancellation_pipe, set_nonblocking};
use super::supervisor::{Control, FailureReporter};
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
            let mut buffer = Vec::new();
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
                    Ok(0) if buffer.is_empty() => {
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
                    Ok(length) => buffer.extend_from_slice(&chunk[..length]),
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(error) => {
                        failures.report(format!("relay stdin read failed: {error}"));
                        return;
                    }
                }
                while let Some(newline) = buffer.iter().position(|byte| *byte == b'\n') {
                    let mut frame = buffer.drain(..=newline).collect::<Vec<_>>();
                    frame.pop();
                    if frame.last() == Some(&b'\r') {
                        frame.pop();
                    }
                    let command = match serde_json::from_slice::<RelayCommand>(&frame) {
                        Ok(command) => command,
                        Err(error) => {
                            failures.report(format!("relay stdin frame is invalid: {error}"));
                            return;
                        }
                    };
                    match command {
                        RelayCommand::Evaluate { language, source } => {
                            if sideband
                                .send(SidebandWrite::Message(ServerMessage::Evaluate {
                                    language,
                                    source,
                                }))
                                .is_err()
                            {
                                failures.report("worker sideband writer stopped".to_string());
                                return;
                            }
                        }
                        RelayCommand::PrepareR { library } => {
                            if sideband
                                .send(SidebandWrite::Message(ServerMessage::PrepareR { library }))
                                .is_err()
                            {
                                failures.report("worker sideband writer stopped".to_string());
                                return;
                            }
                        }
                        RelayCommand::RResolved { library } => {
                            if sideband
                                .send(SidebandWrite::Message(ServerMessage::RResolved { library }))
                                .is_err()
                            {
                                failures.report("worker sideband writer stopped".to_string());
                                return;
                            }
                        }
                        RelayCommand::RResolutionFailed { failure, message } => {
                            if sideband
                                .send(SidebandWrite::Message(ServerMessage::RResolutionFailed {
                                    failure,
                                    message,
                                }))
                                .is_err()
                            {
                                failures.report("worker sideband writer stopped".to_string());
                                return;
                            }
                        }
                        RelayCommand::PreparePython { packages } => {
                            if sideband
                                .send(SidebandWrite::Message(ServerMessage::PreparePython {
                                    packages,
                                }))
                                .is_err()
                            {
                                failures.report("worker sideband writer stopped".to_string());
                                return;
                            }
                        }
                        RelayCommand::PythonResolved { python, native } => {
                            if sideband
                                .send(SidebandWrite::Message(ServerMessage::PythonResolved {
                                    python,
                                    native,
                                }))
                                .is_err()
                            {
                                failures.report("worker sideband writer stopped".to_string());
                                return;
                            }
                        }
                        RelayCommand::PythonResolutionFailed { message } => {
                            if sideband
                                .send(SidebandWrite::Message(
                                    ServerMessage::PythonResolutionFailed { message },
                                ))
                                .is_err()
                            {
                                failures.report("worker sideband writer stopped".to_string());
                                return;
                            }
                        }
                        RelayCommand::PythonVersionResolved { version } => {
                            if sideband
                                .send(SidebandWrite::Message(
                                    ServerMessage::PythonVersionResolved { version },
                                ))
                                .is_err()
                            {
                                failures.report("worker sideband writer stopped".to_string());
                                return;
                            }
                        }
                        RelayCommand::PythonVersionResolutionFailed { message } => {
                            if sideband
                                .send(SidebandWrite::Message(
                                    ServerMessage::PythonVersionResolutionFailed { message },
                                ))
                                .is_err()
                            {
                                failures.report("worker sideband writer stopped".to_string());
                                return;
                            }
                        }
                        RelayCommand::Stdin { data } => {
                            if stdin.send(StdinWrite::Write(data.into_bytes())).is_err() {
                                failures.report("worker stdin writer stopped".to_string());
                                return;
                            }
                        }
                        RelayCommand::Interrupt { request_id } => {
                            if controls.send(Control::Interrupt { request_id }).is_err() {
                                failures.report("relay supervisor stopped".to_string());
                                return;
                            }
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
                    }
                }
                buffer.shrink_to(READ_CHUNK_SIZE);
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
