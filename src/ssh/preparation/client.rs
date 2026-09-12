use std::collections::VecDeque;
use std::io::{self, Read};
use std::process::Stdio;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicU64, Ordering},
    mpsc,
};
use std::thread;
use std::time::{Duration, Instant};

use super::{Discovery, Input, Operation, Output, Selections};
use crate::resolver::{ResolverControl, ResolverControlOutcome, ResolverStopHandle};
use crate::ssh::launch_io::Io;

#[derive(Clone)]
pub(crate) struct Preparation(Arc<Connection>);

struct Connection {
    events: mpsc::Sender<Event>,
    sequence: AtomicU64,
    closed: AtomicBool,
    blocked: Arc<Mutex<Option<String>>>,
}

impl Drop for Connection {
    fn drop(&mut self) {
        let _ = self.events.send(Event::Close(None));
    }
}

#[derive(Default)]
struct State {
    outcome: Mutex<Option<ResolverControlOutcome>>,
    confirmed: AtomicBool,
    finished: AtomicBool,
}

struct Control {
    id: u64,
    events: mpsc::Sender<Event>,
    state: Arc<State>,
}

impl ResolverControl for Control {
    fn stop(&self) -> Result<(), String> {
        if !self.state.finished.load(Ordering::SeqCst) {
            self.events
                .send(Event::Control {
                    id: self.id,
                    control: ResolverControlOutcome::Cancelled,
                    reply: None,
                })
                .map_err(|_| "SSH preparation owner stopped".to_string())?;
        }
        Ok(())
    }
    fn interrupt(&self) -> Result<bool, String> {
        if self.state.finished.load(Ordering::SeqCst) {
            return Ok(false);
        }
        let (reply, response) = mpsc::channel();
        if self
            .events
            .send(Event::Control {
                id: self.id,
                control: ResolverControlOutcome::Interrupted,
                reply: Some(reply),
            })
            .is_err()
        {
            return Ok(false);
        }
        response
            .recv()
            .map_err(|_| "SSH preparation control lost its acknowledgment".to_string())?
    }
    fn control_outcome(&self) -> Option<ResolverControlOutcome> {
        *self.state.outcome.lock().expect("preparation control lock")
    }
    fn cleanup_confirmed(&self) -> bool {
        self.state.confirmed.load(Ordering::SeqCst)
    }
}

type Reply = mpsc::Sender<Result<serde_json::Value, String>>;
type ControlReply = mpsc::Sender<Result<bool, String>>;
enum Event {
    Run {
        id: u64,
        operation: Operation,
        state: Arc<State>,
        reply: Reply,
    },
    Control {
        id: u64,
        control: ResolverControlOutcome,
        reply: Option<ControlReply>,
    },
    Received(Result<Output, String>),
    WriteFailed(String),
    Exited,
    Close(Option<mpsc::Sender<Result<(), String>>>),
}

struct Pending {
    id: u64,
    state: Arc<State>,
    reply: Reply,
    error: Option<String>,
}

impl Preparation {
    pub(crate) fn open(
        session: &crate::ssh::Session,
        selections: Selections,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, Discovery), String> {
        let mut command = session.command_for("ssh-prepare")?;
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        crate::process_descriptors::close_unlisted_from_multithreaded_parent(&mut command)?;
        let mut child = command
            .spawn()
            .map_err(|error| format!("cannot start SSH preparation: {error}"))?;
        let (events, received) = mpsc::channel();
        let (outgoing, writes) = mpsc::channel();
        let (aborted, abort) = io::pipe().map_err(|e| e.to_string())?;
        let stdout = child.stdout.take().expect("SSH preparation stdout");
        let stdin = child.stdin.take().expect("SSH preparation stdin");
        let reader_abort = aborted.try_clone().map_err(|e| e.to_string())?;
        let read_events = events.clone();
        let reader = thread::spawn(move || {
            let result = (|| {
                let mut input = Io::new(stdout, Some(reader_abort), None)?;
                loop {
                    let message = super::read(&mut input)?;
                    let closed = matches!(message, Output::Closed);
                    if closed && input.read(&mut [0]).map_err(|error| error.to_string())? != 0 {
                        return Err("unexpected stdout after SSH preparation shutdown".into());
                    }
                    read_events
                        .send(Event::Received(Ok(message)))
                        .map_err(|_| "SSH preparation owner stopped")?;
                    if closed {
                        return Ok::<(), String>(());
                    }
                }
            })();
            if let Err(error) = result {
                let _ = read_events.send(Event::Received(Err(error)));
            }
        });
        let write_events = events.clone();
        let writer = thread::spawn(move || {
            let result = (|| {
                let mut output = Io::new(stdin, Some(aborted), None)?;
                for message in writes {
                    super::write(&mut output, &message)?;
                }
                Ok::<(), String>(())
            })();
            if let Err(error) = result {
                let _ = write_events.send(Event::WriteFailed(error));
            }
        });
        let exit_events = events.clone();
        let mut exit =
            crate::process_exit::ChildExitWaiter::start_notifying(child.id(), move || {
                let _ = exit_events.send(Event::Exited);
            })?;
        let state = Arc::new(State::default());
        let (reply, response) = mpsc::channel();
        let connection = Self(Arc::new(Connection {
            events: events.clone(),
            sequence: AtomicU64::new(1),
            closed: AtomicBool::new(false),
            blocked: session.blocked.clone(),
        }));
        let pending = Pending {
            id: 0,
            state: state.clone(),
            reply,
            error: None,
        };
        let blocked = session.blocked.clone();
        let open = Input::Open {
            version: super::VERSION,
            build: env!("CARGO_PKG_VERSION").into(),
            workspace: session.target.workspace.clone(),
            selections,
        };
        thread::spawn(move || {
            let _ = run(received, &outgoing, pending, open, &blocked);
            drop(outgoing);
            drop(abort);
            let _ = writer.join();
            let _ = reader.join();
            if !exit.wait(Duration::from_secs(6)).unwrap_or(false) {
                let _ = child.kill();
            }
            let _ = child.wait();
        });
        let handle = ResolverStopHandle::new(Control {
            id: 0,
            events,
            state,
        });
        if let Err(error) = on_started(handle.clone()) {
            let _ = handle.stop();
            let _ = connection.close();
            return Err(error);
        }
        let discovery = response
            .recv()
            .map_err(|_| "SSH preparation discovery stopped".to_string())
            .and_then(|result| result)
            .and_then(|discovery| {
                serde_json::from_value(discovery)
                    .map_err(|error| format!("invalid remote capability result: {error}"))
            });
        match discovery {
            Ok(discovery) => Ok((connection, discovery)),
            Err(error) => {
                // Startup has no Client to own shutdown after discovery fails.
                // Finish the close handshake before the MCP process can exit.
                let _ = connection.close();
                Err(error)
            }
        }
    }

    pub(crate) fn call<T: serde::de::DeserializeOwned>(
        &self,
        operation: Operation,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<T, String> {
        if let Some(error) = &*self.0.blocked.lock().map_err(|_| "SSH session lock")? {
            return Err(error.clone());
        }
        let id = self.0.sequence.fetch_add(1, Ordering::SeqCst);
        let state = Arc::new(State::default());
        let handle = ResolverStopHandle::new(Control {
            id,
            events: self.0.events.clone(),
            state: state.clone(),
        });
        let (reply, response) = mpsc::channel();
        self.0
            .events
            .send(Event::Run {
                id,
                operation,
                state,
                reply,
            })
            .map_err(|_| "SSH preparation owner stopped")?;
        if let Err(error) = on_started(handle.clone()) {
            let _ = handle.stop();
            let _ = response.recv();
            return Err(error);
        }
        let value = response
            .recv()
            .map_err(|_| "SSH preparation owner stopped".to_string())??;
        serde_json::from_value(value).map_err(|error| {
            let error = format!("invalid remote preparation result: {error}");
            *self.0.blocked.lock().expect("SSH session lock") = Some(error.clone());
            error
        })
    }

    pub(crate) fn close(&self) -> Result<(), String> {
        if self.0.closed.swap(true, Ordering::SeqCst) {
            return Ok(());
        }
        let (reply, response) = mpsc::channel();
        self.0
            .events
            .send(Event::Close(Some(reply)))
            .map_err(|_| "SSH preparation owner stopped")?;
        response
            .recv()
            .map_err(|_| "SSH preparation shutdown lost its acknowledgment".to_string())?
    }
}

fn run(
    received: mpsc::Receiver<Event>,
    outgoing: &mpsc::Sender<Input>,
    initial: Pending,
    open: Input,
    blocked: &Mutex<Option<String>>,
) -> Result<(), String> {
    let mut active = Some(initial);
    let mut controls: VecDeque<(u64, ResolverControlOutcome, Option<ControlReply>)> =
        VecDeque::new();
    let mut closing = Vec::new();
    let mut close_requested = false;
    let mut hello = false;
    let mut deadline = Some(Instant::now() + crate::ssh::SETUP_TIMEOUT);
    let result = (|| {
        outgoing
            .send(open)
            .map_err(|_| "SSH preparation writer stopped")?;
        loop {
            let event = match deadline {
                Some(deadline) => received
                    .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                    .map_err(|_| "SSH preparation setup or retirement deadline exceeded")?,
                None => received
                    .recv()
                    .map_err(|_| "SSH preparation owner stopped")?,
            };
            match event {
                Event::Run {
                    id,
                    operation,
                    state,
                    reply,
                } if active.is_none() && !close_requested => {
                    active = Some(Pending {
                        id,
                        state,
                        reply,
                        error: None,
                    });
                    outgoing
                        .send(Input::Run { id, operation })
                        .map_err(|_| "SSH preparation writer stopped")?;
                }
                Event::Control { id, control, reply } => {
                    if active.as_ref().is_some_and(|pending| pending.id == id) {
                        outgoing
                            .send(Input::Control { id, control })
                            .map_err(|_| "SSH preparation writer stopped")?;
                        if control == ResolverControlOutcome::Cancelled {
                            deadline = Some(Instant::now() + Duration::from_secs(7));
                        }
                        controls.push_back((id, control, reply));
                    } else if let Some(reply) = reply {
                        let _ = reply.send(Ok(false));
                    }
                }
                Event::Received(Ok(Output::Hello { version, build })) if !hello => {
                    if version != super::VERSION || build != env!("CARGO_PKG_VERSION") {
                        return Err("incompatible SSH preparation protocol or Console build".into());
                    }
                    hello = true;
                }
                Event::Received(Ok(Output::Controlled { id, result })) if hello => {
                    let Some((expected, control, reply)) = controls.pop_front() else {
                        return Err("unsolicited SSH preparation control acknowledgment".into());
                    };
                    if id != expected {
                        return Err("mismatched SSH preparation control acknowledgment".into());
                    }
                    if result == Ok(true)
                        && let Some(pending) = active.as_ref().filter(|pending| pending.id == id)
                    {
                        pending
                            .state
                            .outcome
                            .lock()
                            .expect("preparation control lock")
                            .get_or_insert(control);
                    }
                    if let Some(reply) = reply {
                        let _ = reply.send(result);
                    }
                }
                Event::Received(Ok(Output::ErrorChunk { id, text })) if hello => {
                    let pending = active
                        .as_mut()
                        .filter(|pending| pending.id == id)
                        .ok_or("mismatched SSH preparation diagnostic")?;
                    pending.error.get_or_insert_default().push_str(&text);
                }
                Event::Received(Ok(Output::Completed {
                    id,
                    result,
                    control,
                    confirmed,
                })) if hello => {
                    let pending = active
                        .as_mut()
                        .filter(|pending| pending.id == id)
                        .ok_or("mismatched SSH preparation result")?;
                    if pending.error.is_some() && result.is_ok() {
                        return Err("SSH preparation diagnostics followed by success".into());
                    }
                    let result = result.map_err(|error| match pending.error.take() {
                        Some(mut prefix) => {
                            prefix.push_str(&error);
                            prefix
                        }
                        None => error,
                    });
                    if !confirmed {
                        return Err("remote preparation process cleanup failed".into());
                    }
                    pending.state.confirmed.store(true, Ordering::SeqCst);
                    pending.state.finished.store(true, Ordering::SeqCst);
                    if let Some(control) = control {
                        pending
                            .state
                            .outcome
                            .lock()
                            .expect("preparation control lock")
                            .get_or_insert(control);
                    }
                    let outcome = *pending
                        .state
                        .outcome
                        .lock()
                        .expect("preparation control lock");
                    let result = result.and_then(|value| match outcome {
                        Some(ResolverControlOutcome::Cancelled) => {
                            Err("remote preparation cancelled".into())
                        }
                        Some(ResolverControlOutcome::Interrupted) => {
                            Err("remote preparation interrupted".into())
                        }
                        None => Ok(value),
                    });
                    let _ = active
                        .take()
                        .expect("active preparation")
                        .reply
                        .send(result);
                    if !close_requested {
                        deadline = None;
                    }
                }
                Event::Close(reply) => {
                    if let Some(reply) = reply {
                        closing.push(reply);
                    }
                    if !close_requested {
                        outgoing
                            .send(Input::Close)
                            .map_err(|_| "SSH preparation writer stopped")?;
                        close_requested = true;
                        deadline = Some(Instant::now() + Duration::from_secs(7));
                    }
                }
                Event::Received(Ok(Output::Closed)) if close_requested && active.is_none() => {
                    return Ok(());
                }
                Event::Received(Err(error)) | Event::WriteFailed(error) => return Err(error),
                // Output and exit are independent transports. A queued terminal
                // frame remains authoritative; the reader reports truncation.
                Event::Exited => {
                    deadline = Some(Instant::now() + Duration::from_secs(1));
                }
                _ => return Err("unexpected SSH preparation event".into()),
            }
        }
    })();
    if let Err(error) = &result {
        *blocked.lock().expect("SSH session lock") = Some(format!(
            "SSH preparation retirement is unconfirmed; this session cannot prepare or start a replacement: {error}"
        ));
    }
    if let Some(pending) = active {
        pending.state.finished.store(true, Ordering::SeqCst);
        let _ = pending.reply.send(Err(format!(
            "SSH preparation retirement is unconfirmed: {}",
            result
                .as_ref()
                .err()
                .map(String::as_str)
                .unwrap_or("missing completion")
        )));
    }
    for (_, _, reply) in controls {
        if let Some(reply) = reply {
            let _ = reply.send(Err("SSH preparation control lost its acknowledgment".into()));
        }
    }
    for reply in closing {
        let _ = reply.send(result.clone());
    }
    result
}
