//! Windows direct-worker owner. Process and pipe events wake blocking waits.
use std::ffi::OsString;
use std::io::{self, BufRead, Read, Write};
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use super::event_writer;
use crate::relay_protocol::{EncodedBytes, RelayCommand, RelayEvent};
use crate::windows::{Event, Pipe};
use crate::worker_protocol::{ServerMessage, WorkerMessage};

enum Control {
    Command(RelayCommand),
    Closed,
    Exited,
    Failed(String),
}

pub(super) fn run(command_line: &[OsString]) -> Result<(), String> {
    let (program, arguments) = command_line
        .split_first()
        .ok_or("worker relay command must include an executable")?;
    let (controls, commands) = mpsc::channel();
    let failed = controls.clone();
    let (events, mut event_writer) = event_writer::start(move |message| {
        let _ = failed.send(Control::Failed(message));
    })?;
    let stopping = Arc::new(AtomicBool::new(false));
    let cancel = Event::new().map_err(|e| e.to_string())?;
    let input_ready = Event::new().map_err(|e| e.to_string())?;
    crate::windows::inherit(input_ready.as_raw_handle(), true).map_err(|e| e.to_string())?;
    let interrupt = Event::new().map_err(|e| e.to_string())?;
    crate::windows::inherit(interrupt.as_raw_handle(), true).map_err(|e| e.to_string())?;
    let (mut sideband, sideband_writer, endpoints) =
        crate::sideband::bind(cancel.clone()).map_err(|e| e.to_string())?;
    let mut command = Command::new(program);
    command
        .args(arguments)
        .creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW)
        .env(
            "MCP_CONSOLE_INTERRUPT_HANDLE",
            (interrupt.as_raw_handle() as usize).to_string(),
        )
        .env(
            "MCP_CONSOLE_INPUT_READY_HANDLE",
            (input_ready.as_raw_handle() as usize).to_string(),
        );
    let (input, writer) = crate::windows::pipe(false, true).map_err(|e| e.to_string())?;
    let (stdout, output) = crate::windows::pipe(true, false).map_err(|e| e.to_string())?;
    let (stderr, diagnostic) = crate::windows::pipe(true, false).map_err(|e| e.to_string())?;
    command
        .stdin(Stdio::from(input))
        .stdout(Stdio::from(output))
        .stderr(Stdio::from(diagnostic));
    endpoints.configure_process(&mut command);
    let mut child = match command.spawn() {
        Ok(child) => ChildOwner(child),
        Err(error) => {
            event_writer.begin_retirement();
            events.send_supervisor(RelayEvent::Fatal {
                message: format!("failed to launch worker: {error}"),
            });
            events.finish();
            return event_writer.join();
        }
    };
    drop(command);
    drop(endpoints);
    crate::windows::inherit(interrupt.as_raw_handle(), false).map_err(|e| e.to_string())?;
    crate::windows::inherit(input_ready.as_raw_handle(), false).map_err(|e| e.to_string())?;
    let exited = controls.clone();
    let _exit = crate::process_exit::ChildExitWaiter::start_notifying(child.id(), move || {
        let _ = exited.send(Control::Exited);
    })?;
    let input_controls = controls.clone();
    // This sole inherited-stdin reader ends with the relay process if the worker
    // exits while its caller still owns stdin. It never outlives a generation.
    thread::spawn(move || {
        for line in io::stdin().lock().split(b'\n') {
            let message =
                line.and_then(|line| serde_json::from_slice(&line).map_err(io::Error::other));
            match message {
                Ok(command) => {
                    if input_controls.send(Control::Command(command)).is_err() {
                        return;
                    }
                }
                Err(error) => {
                    let _ = input_controls.send(Control::Failed(format!(
                        "relay stdin frame is invalid: {error}"
                    )));
                    return;
                }
            }
        }
        let _ = input_controls.send(Control::Closed);
    });
    let mut tasks = Vec::new();
    for (handle, is_error) in [(stdout, false), (stderr, true)] {
        let mut stream = Pipe::from(handle).with_cancel(cancel.clone());
        let output_events = events.clone();
        let output_controls = controls.clone();
        tasks.push(thread::spawn(move || {
            let mut bytes = [0; 8192];
            loop {
                match stream.read(&mut bytes) {
                    Ok(0) => break,
                    Err(error) => {
                        if error.kind() != io::ErrorKind::ConnectionAborted {
                            let _ = output_controls.send(Control::Failed(format!(
                                "worker output read failed: {error}"
                            )));
                        }
                        let mut remaining = stream.available().unwrap_or(0);
                        stream.clear_cancel();
                        while remaining > 0 {
                            let limit = remaining.min(bytes.len());
                            let Ok(n) = stream.read(&mut bytes[..limit]) else {
                                break;
                            };
                            if n == 0 {
                                break;
                            }
                            remaining -= n;
                            let data = EncodedBytes::from_bytes(&bytes[..n]);
                            if !output_events.send(if is_error {
                                RelayEvent::StderrBytes { data }
                            } else {
                                RelayEvent::StdoutBytes { data }
                            }) {
                                break;
                            }
                        }
                        break;
                    }
                    Ok(n) => {
                        let data = EncodedBytes::from_bytes(&bytes[..n]);
                        if !output_events.send(if is_error {
                            RelayEvent::StderrBytes { data }
                        } else {
                            RelayEvent::StdoutBytes { data }
                        }) {
                            break;
                        }
                    }
                }
            }
        }));
    }
    let sideband_events = events.clone();
    let sideband_controls = controls.clone();
    tasks.push(thread::spawn(move || {
        loop {
            match sideband.receive::<WorkerMessage>() {
                Ok(message) => {
                    if !sideband_events.send(message.into()) {
                        break;
                    }
                }
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::UnexpectedEof | io::ErrorKind::ConnectionAborted
                    ) =>
                {
                    break;
                }
                Err(error) => {
                    let _ = sideband_controls.send(Control::Failed(format!(
                        "worker sideband read failed: {error}"
                    )));
                    return;
                }
            }
        }
        let _ = sideband_controls.send(Control::Closed);
    }));
    let (send_sideband, messages) = mpsc::channel::<ServerMessage>();
    let writer_controls = controls.clone();
    let writer_stopping = stopping.clone();
    tasks.push(thread::spawn(move || {
        for message in messages {
            if let Err(error) = sideband_writer.send(&message) {
                if !writer_stopping.load(Ordering::SeqCst) {
                    let _ = writer_controls.send(Control::Failed(format!(
                        "worker sideband write failed: {error}"
                    )));
                }
                break;
            }
        }
    }));
    let (send_stdin, input) = mpsc::channel::<String>();
    let mut stdin = Pipe::from(writer).with_cancel(cancel.clone());
    tasks.push(thread::spawn(move || {
        'input: for bytes in input {
            // Wake both before a potentially blocking write and after bytes have
            // arrived. Small chunks let callbacks make progress on a full pipe.
            for chunk in bytes.as_bytes().chunks(8192) {
                input_ready.set();
                let written = stdin.write_all(chunk);
                input_ready.set();
                if written.is_err() {
                    break 'input;
                }
            }
        }
        drop(stdin);
        input_ready.set();
    }));
    let mut send_stdin = Some(send_stdin);
    let mut deadline = None::<Instant>;
    let mut failure = None;
    loop {
        let next = match deadline {
            Some(deadline) => {
                commands.recv_timeout(deadline.saturating_duration_since(Instant::now()))
            }
            None => commands
                .recv()
                .map_err(|_| mpsc::RecvTimeoutError::Disconnected),
        };
        match next {
            Ok(Control::Exited) => break,
            Err(_) => {
                let _ = child.kill();
                break;
            }
            Ok(Control::Failed(message)) => {
                failure = Some(message);
                let _ = child.kill();
                break;
            }
            Ok(Control::Closed) => {
                if deadline.is_none() {
                    stopping.store(true, Ordering::SeqCst);
                    deadline = Some(Instant::now() + Duration::from_secs(1));
                    send_stdin.take();
                    let _ = send_sideband.send(ServerMessage::Shutdown);
                }
            }
            Ok(Control::Command(command)) => match command {
                RelayCommand::Interrupt { request_id } => {
                    interrupt.set();
                    events.send_supervisor(RelayEvent::InterruptResult {
                        request_id,
                        error: None,
                    });
                }
                RelayCommand::Shutdown { grace_millis } => {
                    stopping.store(true, Ordering::SeqCst);
                    events.send_supervisor(RelayEvent::ShutdownStarted);
                    deadline = Some(Instant::now() + Duration::from_millis(grace_millis));
                    send_stdin.take();
                    let _ = send_sideband.send(ServerMessage::Shutdown);
                }
                RelayCommand::Stdin { data } => {
                    if let Some(stdin) = &send_stdin {
                        let _ = stdin.send(data);
                    }
                }
                RelayCommand::Evaluate { language, source } => {
                    let _ = send_sideband.send(ServerMessage::Evaluate { language, source });
                }
                RelayCommand::PrepareR { library } => {
                    let _ = send_sideband.send(ServerMessage::PrepareR { library });
                }
                RelayCommand::RResolved { library } => {
                    let _ = send_sideband.send(ServerMessage::RResolved { library });
                }
                RelayCommand::RResolutionFailed { failure, message } => {
                    let _ =
                        send_sideband.send(ServerMessage::RResolutionFailed { failure, message });
                }
                RelayCommand::PreparePython { packages } => {
                    let _ = send_sideband.send(ServerMessage::PreparePython { packages });
                }
                RelayCommand::PythonResolved { python } => {
                    let _ = send_sideband.send(ServerMessage::PythonResolved { python });
                }
                RelayCommand::PythonResolutionFailed { message } => {
                    let _ = send_sideband.send(ServerMessage::PythonResolutionFailed { message });
                }
                RelayCommand::PythonVersionResolved { version } => {
                    let _ = send_sideband.send(ServerMessage::PythonVersionResolved { version });
                }
                RelayCommand::PythonVersionResolutionFailed { message } => {
                    let _ = send_sideband
                        .send(ServerMessage::PythonVersionResolutionFailed { message });
                }
            },
        }
    }
    let status = child.wait().map_err(|e| e.to_string())?;
    event_writer.begin_retirement();
    cancel.set();
    drop(send_stdin);
    drop(send_sideband);
    for task in tasks {
        task.join().map_err(|_| "worker I/O task panicked")?;
    }
    events.send_supervisor(RelayEvent::StdoutClosed);
    events.send_supervisor(RelayEvent::StderrClosed);
    events.send_supervisor(RelayEvent::WorkerSidebandClosed);
    if let Some(message) = failure {
        events.send_supervisor(RelayEvent::Fatal { message });
    }
    events.send_supervisor(RelayEvent::WorkerExited {
        code: status.code().unwrap_or(1),
    });
    events.finish();
    event_writer.join()
}

// Any error after spawning must still retire the one directly owned worker.
struct ChildOwner(std::process::Child);
impl std::ops::Deref for ChildOwner {
    type Target = std::process::Child;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}
impl std::ops::DerefMut for ChildOwner {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.0
    }
}
impl Drop for ChildOwner {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
