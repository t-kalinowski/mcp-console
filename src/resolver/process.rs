//! Shared resolver control and result collection over native process mechanics.

#[cfg(unix)]
mod unix;
#[cfg(windows)]
mod windows;
#[cfg(unix)]
use unix as native;
#[cfg(windows)]
use windows as native;

use native::Child;
pub(crate) use native::resolver_command;
use native::{interrupt_resolver, stop_resolver};

use std::io;
use std::io::{Read, Write};
use std::path::Path;
use std::process::ExitStatus;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};

use crate::process_exit::ChildExitWaiter;

#[derive(Clone)]
pub(crate) struct ResolverStopHandle(Arc<dyn ResolverControl>);

pub(crate) trait ResolverControl: Send + Sync {
    fn stop(&self) -> Result<(), String>;
    fn interrupt(&self) -> Result<bool, String>;
    fn control_outcome(&self) -> Option<super::ResolverControlOutcome>;
    fn cleanup_confirmed(&self) -> bool;
    /// Setup consumers must retain an independent operation failure even when
    /// a control and confirmed cleanup accompanied it.
    fn failure_is_controlled(&self) -> bool {
        self.control_outcome().is_some()
    }
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
    pub(crate) fn failure_is_controlled(&self) -> bool {
        self.0.failure_is_controlled()
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
    IoFailed(String),
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

    pub(crate) fn spawn(
        &self,
        command: &mut Command,
        input: Option<Vec<u8>>,
    ) -> io::Result<ResolverInvocation> {
        let endpoints = native::prepare_io(command, input.is_some())?;
        let child = native::spawn_resolver(command);
        // Command owns the synchronous child endpoints, too. Release them on
        // both spawn results before any I/O or EOF observation can begin.
        command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        let child = child?;
        self.cleanup.store(false, Ordering::SeqCst);
        *self.waiting.lock().expect("resolver phase lock") = true;
        let events = self.events.clone();
        let observation = match ChildExitWaiter::start_cancellable(child.id(), move |result| {
            let _ = events.send(ResolverEvent::Exited(result));
        }) {
            Ok(exit) => Some(exit),
            Err(error) => {
                let _ = self.events.send(ResolverEvent::Exited(Err(error)));
                None
            }
        };
        let stdout = read_output(endpoints.stdout, self.events.clone(), "stdout");
        let stderr = read_output(endpoints.stderr, self.events.clone(), "stderr");
        let input = input.map(|bytes| {
            let mut writer = endpoints.input.expect("resolver input endpoint");
            let events = self.events.clone();
            thread::spawn(move || {
                let result = writer.write_all(&bytes);
                if let Err(error) = &result {
                    let _ = events.send(ResolverEvent::IoFailed(format!(
                        "failed to write resolver stdin: {error}"
                    )));
                }
                result
            })
        });
        Ok(ResolverInvocation {
            child,
            observation,
            input,
            stdout,
            stderr,
            input_cancel: endpoints.input_cancel,
            output_cancel: endpoints.output_cancel,
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
                ResolverEvent::Exited(_) | ResolverEvent::IoFailed(_) => {}
            }
        }
        *waiting = false;
        if cancelled {
            Err(format!("{kind} resolution cancelled"))
        } else {
            Ok(())
        }
    }

    pub(crate) fn collect(
        &self,
        mut invocation: ResolverInvocation,
        program: &Path,
        kind: &str,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<ResolverOutput, String> {
        let primary = on_started(self.stop_handle())
            .and_then(|()| wait_for_resolver_exit(&mut invocation.child, self, program, kind))
            .err();
        let mut failure = ResolverFailure {
            primary,
            cleanup: Vec::new(),
        };
        let retirement = invocation.retire(program, kind);
        self.cleanup.store(retirement.confirmed(), Ordering::SeqCst);
        if let Err(error) = &retirement.process {
            failure.record_cleanup(error.clone());
        }
        if let Err(error) = &retirement.observation {
            failure.record_cleanup(error.clone());
        }
        if let Ok(Some(error)) = &retirement.observation {
            failure.record_cleanup(format!(
                "failed to wait for {kind} resolver `{}`: {error}",
                program.display()
            ));
        }
        if let Err(error) = self.finish_wait(kind) {
            failure.primary.get_or_insert(error);
        }
        let write_result = match retirement.input {
            None => Ok(()),
            Some(Ok(result)) => result,
            Some(Err(_)) => {
                failure.record_cleanup(format!("{kind} resolver stdin writer task failed"));
                Ok(())
            }
        };
        let stdout = collect_output(retirement.stdout, "stdout", &mut failure);
        let stderr = collect_output(retirement.stderr, "stderr", &mut failure);
        if let Some(mut error) = failure.into_message() {
            // A cleanup error must not erase the operation's captured diagnostic.
            let diagnostic = String::from_utf8_lossy(&stderr);
            let ordinary = String::from_utf8_lossy(&stdout);
            let detail = if diagnostic.trim().is_empty() {
                ordinary.trim()
            } else {
                diagnostic.trim()
            };
            if !detail.is_empty() {
                error.push_str(": ");
                error.push_str(detail);
            }
            return Err(error);
        }
        Ok(ResolverOutput {
            status: retirement.process.expect("confirmed resolver status"),
            write_result,
            stdout,
            stderr,
        })
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

struct Endpoints {
    input: Option<Box<dyn Write + Send>>,
    stdout: Box<dyn Read + Send>,
    stderr: Box<dyn Read + Send>,
    input_cancel: native::Cancel,
    output_cancel: native::Cancel,
}

/// Evidence only for this materializer's native process scope and owned tasks;
/// it says nothing about preparation transport or worker retirement.
struct ResolverRetirement {
    process: Result<ExitStatus, String>,
    observation: Result<Option<String>, String>,
    input: Option<thread::Result<io::Result<()>>>,
    stdout: thread::Result<(Vec<u8>, io::Result<()>)>,
    stderr: thread::Result<(Vec<u8>, io::Result<()>)>,
}

impl ResolverRetirement {
    fn confirmed(&self) -> bool {
        self.process.is_ok()
            && self.observation.is_ok()
            && self.input.as_ref().is_none_or(Result::is_ok)
            && self.stdout.is_ok()
            && self.stderr.is_ok()
    }
}

/// Owned resources for one subprocess, distinct from reusable control state.
/// Process confirmation never stands in for observation or I/O task settlement.
pub(crate) struct ResolverInvocation {
    child: Child,
    observation: Option<ChildExitWaiter>,
    input: Option<JoinHandle<io::Result<()>>>,
    stdout: JoinHandle<(Vec<u8>, io::Result<()>)>,
    stderr: JoinHandle<(Vec<u8>, io::Result<()>)>,
    input_cancel: native::Cancel,
    output_cancel: native::Cancel,
}

impl ResolverInvocation {
    fn retire(mut self, program: &Path, kind: &str) -> ResolverRetirement {
        // Registration, process and I/O failures share this path. Wake stdin
        // independently, then retain the output tail through native retirement.
        drop(self.input_cancel);
        let process = stop_resolver(&mut self.child, program, kind, self.observation.as_mut());
        let observation = self
            .observation
            .as_mut()
            .map_or(Ok(None), ChildExitWaiter::cancel_and_finish);
        drop(self.output_cancel);
        // Join every task before publishing any terminal evidence, even when
        // process retirement failed. A joined task may itself report I/O error.
        ResolverRetirement {
            process,
            observation,
            input: self.input.map(|task| task.join()),
            stdout: self.stdout.join(),
            stderr: self.stderr.join(),
        }
    }
}

fn read_output(
    mut output: Box<dyn Read + Send>,
    events: Sender<ResolverEvent>,
    name: &'static str,
) -> JoinHandle<(Vec<u8>, io::Result<()>)> {
    thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = output.read_to_end(&mut bytes).map(|_| ());
        if let Err(error) = &result {
            let _ = events.send(ResolverEvent::IoFailed(format!(
                "failed to read resolver {name}: {error}"
            )));
        }
        (bytes, result)
    })
}

struct ResolverFailure {
    primary: Option<String>,
    cleanup: Vec<String>,
}

impl ResolverFailure {
    fn record_cleanup(&mut self, error: String) {
        if self.primary.as_ref() != Some(&error) && !self.cleanup.contains(&error) {
            self.cleanup.push(error);
        }
    }

    fn into_message(self) -> Option<String> {
        let errors: Vec<_> = self.primary.into_iter().chain(self.cleanup).collect();
        (!errors.is_empty()).then(|| errors.join("; "))
    }
}

fn collect_output(
    result: thread::Result<(Vec<u8>, io::Result<()>)>,
    name: &str,
    failure: &mut ResolverFailure,
) -> Vec<u8> {
    match result {
        Ok((bytes, result)) => {
            if let Err(error) = result {
                failure.record_cleanup(format!("failed to read resolver {name}: {error}"));
            }
            bytes
        }
        Err(_) => {
            failure.record_cleanup(format!("resolver {name} reader task failed"));
            Vec::new()
        }
    }
}

fn wait_for_resolver_exit(
    child: &mut Child,
    resolver: &ResolverProcess,
    program: &Path,
    kind: &str,
) -> Result<(), String> {
    loop {
        match resolver.event_receiver.recv() {
            Ok(ResolverEvent::Cancel) => return Err(format!("{kind} resolution cancelled")),
            Ok(ResolverEvent::Interrupt {
                reply,
                clear_marker,
            }) => match interrupt_resolver(child) {
                Ok(ResolverInterrupt::Signaled | ResolverInterrupt::AlreadyExited) => {
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
                    return Err(message);
                }
            },
            Ok(ResolverEvent::Exited(result)) => {
                return result.map_err(|error| {
                    format!(
                        "failed to wait for {kind} resolver `{}`: {error}",
                        program.display()
                    )
                });
            }
            Ok(ResolverEvent::IoFailed(error)) => return Err(error),
            Err(_) => return Err(format!("{kind} resolver exit task stopped")),
        }
    }
}

fn settle_observation(exit: Option<&mut ChildExitWaiter>) {
    if let Some(exit) = exit {
        // Exit/error is delivered through ResolverEvent::Exited. Cancellation
        // still owns termination and cleanup; this barrier only settles the
        // native observation and its event before reaping releases identity.
        let _ = exit.finish();
    }
}
