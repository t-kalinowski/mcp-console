use std::collections::VecDeque;
#[cfg(unix)]
use std::io::{self, Write};
use std::io::{BufReader, Read};
use std::process::{Child, Stdio};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicU64, Ordering},
    mpsc,
};
use std::thread;
use std::time::{Duration, Instant};

use super::{Discovery, Input, Mode, Operation, Output, Selections};
use crate::resolver::{ResolverControl, ResolverControlOutcome, ResolverStopHandle};
#[cfg(unix)]
use crate::target_launch::transfer::Io;

#[derive(Clone)]
pub(crate) struct Preparation(Arc<Connection>);

struct Connection {
    events: mpsc::Sender<Event>,
    sequence: AtomicU64,
    owner: Mutex<Option<thread::JoinHandle<Result<(), String>>>>,
    unconfirmed: Arc<Mutex<Option<UnconfirmedChild>>>,
    blocked: Arc<Mutex<Option<String>>>,
    local: bool,
}

struct UnconfirmedChild {
    child: Arc<Mutex<Option<Child>>>,
    done: mpsc::Receiver<()>,
    reaper: Option<thread::JoinHandle<()>>,
}

impl UnconfirmedChild {
    fn retain(child: Child, mut exit: crate::process_exit::ChildExitWaiter) -> Self {
        let child = Arc::new(Mutex::new(Some(child)));
        let retained = child.clone();
        let (finished, done) = mpsc::channel();
        let reaper = thread::spawn(move || {
            // Keep the process handle and its observer owned after the bounded
            // caller returns. Exit observation precedes the sole reap.
            if exit.finish().is_ok() {
                let mut retained = retained.lock().expect("preparation child lock");
                if retained
                    .as_mut()
                    .expect("unconfirmed preparation child")
                    .try_wait()
                    .is_ok_and(|status| status.is_some())
                {
                    retained.take();
                }
            }
            let _ = finished.send(());
        });
        Self {
            child,
            done,
            reaper: Some(reaper),
        }
    }

    fn retry(&mut self, local: bool) -> Result<(), String> {
        if let Some(child) = self.child.lock().expect("preparation child lock").as_mut() {
            child.kill().map_err(|error| {
                format!(
                    "cannot terminate {}: {error}; retirement unconfirmed",
                    label(local)
                )
            })?;
        }
        if self.reaper.is_some() {
            self.done
                .recv_timeout(Duration::from_secs(2))
                .map_err(|_| format!("{} retirement unconfirmed", label(local)))?;
            self.reaper
                .take()
                .expect("preparation reaper")
                .join()
                .map_err(|_| format!("{} reaper panicked", label(local)))?;
        }
        if self.child.lock().expect("preparation child lock").is_some() {
            return Err(format!("{} retirement unconfirmed", label(local)));
        }
        Ok(())
    }
}

impl Drop for Connection {
    fn drop(&mut self) {
        let _ = self.events.send(Event::Close);
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
    local: bool,
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
                .map_err(|_| format!("{} owner stopped", label(self.local)))?;
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
            .map_err(|_| format!("{} control lost its acknowledgment", label(self.local)))?
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
        request: Input,
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
    Close,
}

struct Pending {
    id: u64,
    state: Arc<State>,
    reply: Reply,
    chunks: Option<String>,
}

fn label(local: bool) -> &'static str {
    if local {
        "local resolver"
    } else {
        "SSH preparation"
    }
}

impl Preparation {
    pub(crate) fn check_ready(&self) -> Result<(), String> {
        if let Some(error) = &*self
            .0
            .blocked
            .lock()
            .map_err(|_| "preparation session lock")?
        {
            return Err(error.clone());
        }
        Ok(())
    }

    pub(crate) fn open(
        session: &crate::ssh::Session,
        selections: Selections,
        diagnostics: crate::process_output::Diagnostics,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, Discovery), String> {
        #[cfg(not(unix))]
        {
            let _ = (session, selections, diagnostics, on_started);
            Err("SSH preparation requires macOS or Linux".into())
        }
        #[cfg(unix)]
        {
            let command = session.command_for("ssh-prepare")?;
            let open = Input::Open {
                build: env!("CARGO_PKG_VERSION").into(),
                workspace: session.target.workspace.clone(),
                selections,
                mode: Mode::Auto,
            };
            Self::open_with(
                command,
                session.blocked.clone(),
                open,
                false,
                diagnostics,
                on_started,
            )
        }
    }

    pub(crate) fn open_local(
        mode: Mode,
        resolver: Option<crate::settings::SandboxSettings>,
        python: Option<&std::ffi::OsStr>,
        diagnostics: crate::process_output::Diagnostics,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, Discovery), String> {
        let mut command = if let Some(mut settings) = resolver {
            // Preparation and the retained worker must agree on whether Python
            // is managed, including with an isolated resolver environment.
            crate::settings::preserve_environment(
                &mut settings,
                [("RETICULATE_PYTHON".as_ref(), python)],
            )?;
            #[cfg(unix)]
            let command = crate::resolver::sandbox::command(settings, std::process::id())?;
            #[cfg(windows)]
            let command = {
                // Windows has no resolver proxy support yet. Keep its existing
                // host execution, but use the captured Console cache environment.
                let mut command = std::process::Command::new(
                    std::env::current_exe().map_err(|error| error.to_string())?,
                );
                if settings.get("inherit_environment") == Some(&serde_json::Value::Bool(false)) {
                    command.env_clear();
                }
                if let Some(environment) = settings.get("environment") {
                    let values: std::collections::BTreeMap<String, String> =
                        serde_json::from_value(environment.clone())
                            .map_err(|error| error.to_string())?;
                    command.envs(values);
                }
                command
            };
            command
        } else {
            std::process::Command::new(std::env::current_exe().map_err(|error| error.to_string())?)
        };
        if let Some(python) = python {
            command.env("RETICULATE_PYTHON", python);
        } else {
            command.env_remove("RETICULATE_PYTHON");
        }
        command.arg("resolve");
        let open = Input::Open {
            build: env!("CARGO_PKG_VERSION").into(),
            workspace: String::new(),
            selections: Selections::default(),
            mode,
        };
        Self::open_with(command, Arc::default(), open, true, diagnostics, on_started)
    }

    fn open_with(
        mut command: std::process::Command,
        blocked: Arc<Mutex<Option<String>>>,
        open: Input,
        local: bool,
        diagnostics: crate::process_output::Diagnostics,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, Discovery), String> {
        let native = command.get_args().next() == Some("sandbox".as_ref());
        #[cfg(unix)]
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            let _ = diagnostics;
            command.stderr(Stdio::inherit());
        }
        #[cfg(unix)]
        crate::process_descriptors::close_unlisted_from_multithreaded_parent(&mut command)?;
        #[cfg(windows)]
        let (aborted, abort) = crate::windows::notification().map_err(|e| e.to_string())?;
        #[cfg(windows)]
        let (stdin, stdout) = crate::windows::command_pipes(&mut command, aborted.clone())
            .map_err(|e| e.to_string())?;
        let mut child = command.spawn().map_err(|error| {
            format!(
                "cannot start {} preparation: {error}",
                if local { "local" } else { "SSH" }
            )
        })?;
        // Windows command_pipes installs owned child-side handles in Command.
        // Release them before discovery failure can wait for shutdown and EOF.
        drop(command);
        let (events, received) = mpsc::channel();
        let (outgoing, writes) = mpsc::channel();
        #[cfg(unix)]
        let (aborted, abort) = io::pipe().map_err(|e| e.to_string())?;
        #[cfg(unix)]
        let stdout = child.stdout.take().expect("preparation stdout");
        #[cfg(unix)]
        let stdin = child.stdin.take().expect("preparation stdin");
        #[cfg(unix)]
        let (diagnostic_exit, mut notify_diagnostic_exit) =
            io::pipe().map_err(|e| e.to_string())?;
        #[cfg(unix)]
        let mut diagnostic_abort = notify_diagnostic_exit
            .try_clone()
            .map_err(|e| e.to_string())?;
        #[cfg(unix)]
        let diagnostic_reader = {
            let stderr = child.stderr.take().expect("preparation stderr");
            let diagnostic_events = events.clone();
            thread::spawn(move || {
                if let Err(error) =
                    crate::process_output::forward(stderr, diagnostic_exit, diagnostics)
                {
                    let _ = diagnostic_events.send(Event::Received(Err(format!(
                        "{} stderr read failed: {error}",
                        label(local)
                    ))));
                }
            })
        };
        #[cfg(unix)]
        let reader_abort = aborted.try_clone().map_err(|e| e.to_string())?;
        #[cfg(windows)]
        let reader_abort = aborted;
        let read_events = events.clone();
        let reader = thread::spawn(move || {
            let result = (|| {
                #[cfg(unix)]
                let mut input = BufReader::new(Io::new(stdout, Some(reader_abort), None)?);
                #[cfg(windows)]
                let mut input = BufReader::new(stdout.with_cancel(reader_abort));
                loop {
                    let message = if local {
                        super::read_jsonl(&mut input)?
                    } else {
                        super::read(&mut input)?
                    };
                    let closed = matches!(message, Output::Closed);
                    if closed && input.read(&mut [0]).map_err(|error| error.to_string())? != 0 {
                        return Err(format!("unexpected stdout after {} shutdown", label(local)));
                    }
                    read_events
                        .send(Event::Received(Ok(message)))
                        .map_err(|_| format!("{} owner stopped", label(local)))?;
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
                #[cfg(unix)]
                let mut output = Io::new(stdin, Some(aborted), None)?;
                #[cfg(windows)]
                let mut output = stdin;
                for message in writes {
                    if local {
                        super::write_jsonl(&mut output, &message)?;
                    } else {
                        super::write(&mut output, &message)?;
                    }
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
                #[cfg(unix)]
                let _ = notify_diagnostic_exit.write_all(&[1]);
                let _ = exit_events.send(Event::Exited);
            })?;
        let state = Arc::new(State::default());
        let (reply, response) = mpsc::channel();
        let pending = Pending {
            id: 0,
            state: state.clone(),
            reply,
            chunks: None,
        };
        let owner_blocked = blocked.clone();
        let unconfirmed = Arc::new(Mutex::new(None));
        let owner_unconfirmed = unconfirmed.clone();
        let owner = thread::spawn(move || {
            let result = run(received, &outgoing, pending, open, &owner_blocked, local);
            drop(outgoing);
            drop(abort);
            // Retire before joining I/O and reaping. Native cleanup needs a
            // bounded SIGTERM allowance; direct execution can stop immediately.
            let mut reaped = false;
            let retired = (|| {
                if result.is_err() || !exit.wait(Duration::from_secs(6)).unwrap_or(false) {
                    #[cfg(unix)]
                    let force = if local && native {
                        // Let the native supervisor retire the resolver tree before
                        // escalation. Killing the supervisor bypasses its cleanup.
                        unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM) };
                        !exit.wait(Duration::from_secs(2)).unwrap_or(false)
                    } else {
                        true
                    };
                    #[cfg(windows)]
                    let force = true;
                    if force {
                        child.kill().map_err(|error| {
                            format!(
                                "cannot terminate {}: {error}; retirement unconfirmed",
                                label(local)
                            )
                        })?;
                        if !exit.wait(Duration::from_secs(2))? {
                            return Err(format!(
                                "{} did not exit after forced termination; retirement unconfirmed",
                                label(local)
                            ));
                        }
                    }
                }
                exit.finish()?;
                let status = child
                    .try_wait()
                    .map_err(|error| format!("cannot reap {}: {error}", label(local)))?
                    .ok_or_else(|| format!("{} retirement unconfirmed", label(local)))?;
                reaped = true;
                if native && !status.success() {
                    Err(format!("{} sandbox exited with {status}", label(local)))
                } else {
                    Ok(())
                }
            })();
            #[cfg(unix)]
            let _ = diagnostic_abort.write_all(&[1]);
            let _ = writer.join();
            let _ = reader.join();
            #[cfg(unix)]
            let _ = diagnostic_reader.join();
            if !reaped {
                *owner_unconfirmed
                    .lock()
                    .expect("preparation retirement lock") =
                    Some(UnconfirmedChild::retain(child, exit));
            }
            let result = match (result, retired) {
                (Err(error), Err(cleanup)) => Err(format!("{error}; {cleanup}")),
                (result, cleanup) => result.and(cleanup),
            };
            if let Err(error) = &result {
                *owner_blocked.lock().expect("preparation session lock") = Some(format!(
                    "{} retirement is unconfirmed; this session cannot prepare or start a replacement: {error}",
                    label(local)
                ));
            }
            result
        });
        let connection = Self(Arc::new(Connection {
            events: events.clone(),
            sequence: AtomicU64::new(1),
            owner: Mutex::new(Some(owner)),
            unconfirmed,
            blocked,
            local,
        }));
        let handle = ResolverStopHandle::new(Control {
            id: 0,
            events,
            state,
            local,
        });
        if let Err(error) = on_started(handle.clone()) {
            let _ = handle.stop();
            let _ = connection.close();
            return Err(error);
        }
        let discovery = response
            .recv()
            .map_err(|_| format!("{} discovery stopped", label(local)))
            .and_then(|result| result)
            .and_then(|discovery| {
                serde_json::from_value(discovery).map_err(|error| {
                    if local {
                        format!("invalid local resolver capability result: {error}")
                    } else {
                        format!("invalid remote capability result: {error}")
                    }
                })
            });
        match discovery {
            Ok(discovery) => Ok((connection, discovery)),
            Err(error) => {
                // Startup has no Client to own shutdown after discovery fails.
                // Join preparation cleanup before the MCP process can exit.
                match connection.close() {
                    Ok(()) => Err(error),
                    Err(cleanup) => Err(format!("{error}; {cleanup}")),
                }
            }
        }
    }

    pub(crate) fn call<T: serde::de::DeserializeOwned>(
        &self,
        operation: Operation,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<T, String> {
        self.check_ready()?;
        let id = self.0.sequence.fetch_add(1, Ordering::SeqCst);
        let request = Input::Run { id, operation };
        // Reject unsendable requests before registering a resolver or admitting
        // remote work. No retirement confirmation is needed for a rejected input.
        super::encode(&request)?;
        let state = Arc::new(State::default());
        let handle = ResolverStopHandle::new(Control {
            id,
            events: self.0.events.clone(),
            state: state.clone(),
            local: self.0.local,
        });
        let (reply, response) = mpsc::channel();
        self.0
            .events
            .send(Event::Run {
                id,
                request,
                state,
                reply,
            })
            .map_err(|_| format!("{} owner stopped", label(self.0.local)))?;
        if let Err(error) = on_started(handle.clone()) {
            let _ = handle.stop();
            let _ = response.recv();
            return Err(error);
        }
        let value = response
            .recv()
            .map_err(|_| format!("{} owner stopped", label(self.0.local)))??;
        serde_json::from_value(value).map_err(|error| {
            let error = if self.0.local {
                format!("invalid local resolver result: {error}")
            } else {
                format!("invalid remote preparation result: {error}")
            };
            *self.0.blocked.lock().expect("preparation session lock") = Some(error.clone());
            error
        })
    }

    pub(crate) fn close(&self) -> Result<(), String> {
        // Hold the lock through the join so every close caller waits for the
        // owner to reap its child, even after Closed and EOF arrive.
        let mut owner = self.0.owner.lock().map_err(|_| "preparation owner lock")?;
        let _ = self.0.events.send(Event::Close);
        let result = owner.take().map_or(Ok(()), |owner| {
            owner
                .join()
                .map_err(|_| format!("{} owner panicked", label(self.0.local)))?
        });
        let retry = self
            .0
            .unconfirmed
            .lock()
            .map_err(|_| "preparation retirement lock")?
            .as_mut()
            .map_or(Ok(()), |child| child.retry(self.0.local));
        // A later exit or successful retry does not erase the original
        // protocol/retirement failure or confirm native descendant cleanup.
        result.and(retry)
    }
}

fn run(
    received: mpsc::Receiver<Event>,
    outgoing: &mpsc::Sender<Input>,
    initial: Pending,
    open: Input,
    blocked: &Mutex<Option<String>>,
    local: bool,
) -> Result<(), String> {
    let owner = label(local);
    let mut active = Some(initial);
    let mut controls: VecDeque<(u64, ResolverControlOutcome, Option<ControlReply>)> =
        VecDeque::new();
    let mut close_requested = false;
    let mut hello = false;
    let mut setup_deadline = (!local).then(|| Instant::now() + super::SETUP_TIMEOUT);
    let mut retirement_deadline = None;
    let result = (|| {
        outgoing
            .send(open)
            .map_err(|_| format!("{owner} writer stopped"))?;
        loop {
            let event = match retirement_deadline.or(setup_deadline) {
                Some(deadline) => received
                    .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                    .map_err(|_| format!("{owner} setup or retirement deadline exceeded"))?,
                None => received
                    .recv()
                    .map_err(|_| format!("{owner} owner stopped"))?,
            };
            match event {
                Event::Run {
                    id,
                    request,
                    state,
                    reply,
                } if active.is_none() && !close_requested => {
                    active = Some(Pending {
                        id,
                        state,
                        reply,
                        chunks: None,
                    });
                    outgoing
                        .send(request)
                        .map_err(|_| format!("{owner} writer stopped"))?;
                }
                Event::Control { id, control, reply } => {
                    // Close already cancels active work and is the final host
                    // request. A concurrent control must not write after it.
                    if !close_requested && active.as_ref().is_some_and(|pending| pending.id == id) {
                        outgoing
                            .send(Input::Control { id, control })
                            .map_err(|_| format!("{owner} writer stopped"))?;
                        if !local && control == ResolverControlOutcome::Cancelled {
                            retirement_deadline = Some(Instant::now() + Duration::from_secs(7));
                        }
                        controls.push_back((id, control, reply));
                    } else if let Some(reply) = reply {
                        let _ = reply.send(Ok(false));
                    }
                }
                Event::Received(Ok(Output::Hello { build })) if !hello => {
                    if build != env!("CARGO_PKG_VERSION") {
                        return Err(format!("incompatible {owner} Console build"));
                    }
                    hello = true;
                    setup_deadline = None;
                }
                Event::Received(Ok(Output::Controlled { id, result })) if hello => {
                    let Some((expected, control, reply)) = controls.pop_front() else {
                        return Err(format!("unsolicited {owner} control acknowledgment"));
                    };
                    if id != expected {
                        return Err(format!("mismatched {owner} control acknowledgment"));
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
                Event::Received(Ok(Output::ResultChunk { id, text })) if hello => {
                    let pending = active
                        .as_mut()
                        .filter(|pending| pending.id == id)
                        .ok_or_else(|| format!("mismatched {owner} result chunk"))?;
                    pending.chunks.get_or_insert_default().push_str(&text);
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
                        .ok_or_else(|| format!("mismatched {owner} result"))?;
                    let result = match (pending.chunks.take(), result) {
                        (None, Some(result)) => result,
                        (Some(chunks), None) => serde_json::from_str(&chunks)
                            .map_err(|error| format!("invalid chunked {owner} result: {error}"))?,
                        _ => return Err(format!("{owner} requires one complete result")),
                    };
                    if !confirmed {
                        return Err(if local {
                            "local resolver process cleanup failed".into()
                        } else {
                            "remote preparation process cleanup failed".into()
                        });
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
                    let control_label = if local {
                        "local resolver"
                    } else {
                        "remote preparation"
                    };
                    let result = result.and_then(|value| match outcome {
                        Some(ResolverControlOutcome::Cancelled) => {
                            Err(format!("{control_label} cancelled"))
                        }
                        Some(ResolverControlOutcome::Interrupted) => {
                            Err(format!("{control_label} interrupted"))
                        }
                        None => Ok(value),
                    });
                    let _ = active
                        .take()
                        .expect("active preparation")
                        .reply
                        .send(result);
                    if !close_requested {
                        retirement_deadline = None;
                    }
                }
                Event::Close => {
                    if !close_requested {
                        outgoing
                            .send(Input::Close)
                            .map_err(|_| format!("{owner} writer stopped"))?;
                        close_requested = true;
                        retirement_deadline = Some(Instant::now() + Duration::from_secs(7));
                    }
                }
                Event::Received(Ok(Output::Closed)) if close_requested && active.is_none() => {
                    return Ok(());
                }
                Event::Received(Err(error)) | Event::WriteFailed(error) => return Err(error),
                // Output and exit are independent transports. A queued terminal
                // frame remains authoritative; the reader reports truncation.
                Event::Exited => {
                    retirement_deadline = Some(Instant::now() + Duration::from_secs(1));
                }
                _ => return Err(format!("unexpected {owner} event")),
            }
        }
    })();
    if let Err(error) = &result {
        *blocked.lock().expect("preparation session lock") = Some(format!(
            "{owner} retirement is unconfirmed; this session cannot prepare or start a replacement: {error}"
        ));
    }
    if let Some(pending) = active {
        pending.state.finished.store(true, Ordering::SeqCst);
        let _ = pending.reply.send(Err(format!(
            "{owner} retirement is unconfirmed: {}",
            result
                .as_ref()
                .err()
                .map(String::as_str)
                .unwrap_or("missing completion")
        )));
    }
    for (_, _, reply) in controls {
        if let Some(reply) = reply {
            let _ = reply.send(Err(format!("{owner} control lost its acknowledgment")));
        }
    }
    result
}
