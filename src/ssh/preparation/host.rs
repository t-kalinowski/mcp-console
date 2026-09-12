use std::io;
use std::path::PathBuf;
use std::sync::{Mutex, mpsc};
use std::thread;
use std::time::Instant;

use super::{Discovery, Input, Operation, Output, Selections};
use crate::resolver::{self, ResolverControlOutcome, ResolverStopHandle};
use crate::ssh::launch_io::{Io, duplicate};

struct Context {
    bootstrap: Option<resolver::ManagedRBootstrap>,
    r: Option<resolver::ManagedRResolverConfiguration>,
    python: resolver::ManagedPythonResolverConfiguration,
    rscript: PathBuf,
    managed_python: bool,
}

impl Context {
    fn discover(
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, Discovery), String> {
        let mut python = resolver::ManagedPythonResolverConfiguration::capture();
        let (bootstrap, rscript) = resolver::discover(&mut python, on_started)?;
        let configured_python = std::env::var("RETICULATE_PYTHON").ok();
        let managed_python = !configured_python
            .as_ref()
            .is_some_and(|python| !python.is_empty() && python != "managed");
        let discovery = Discovery {
            managed: bootstrap.is_some(),
            selections: Selections {
                r_home: Some(
                    rscript
                        .parent()
                        .and_then(std::path::Path::parent)
                        .ok_or("remote Rscript has no R home")?
                        .to_string_lossy()
                        .into_owned(),
                ),
                python: configured_python,
            },
        };
        Ok((
            Self {
                bootstrap,
                r: None,
                python,
                rscript,
                managed_python,
            },
            discovery,
        ))
    }

    fn execute(
        &mut self,
        operation: Operation,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<serde_json::Value, String> {
        match operation {
            Operation::Bootstrap => {
                let bootstrap = self
                    .bootstrap
                    .as_ref()
                    .ok_or("remote dynamic environment resolution is unavailable")?;
                // Capture choices once; a failed selected bootstrap remains an error.
                if self.r.is_none() {
                    self.r = Some(bootstrap.prepare(&mut self.python, on_started)?);
                }
                Ok(serde_json::Value::Null)
            }
            Operation::R { requirements } => {
                let configuration = self
                    .r
                    .as_ref()
                    .ok_or("remote R bootstrap has not been prepared")?;
                let r = resolver::resolve_r_with(configuration, requirements, on_started)?;
                serde_json::to_value(r).map_err(|error| error.to_string())
            }
            Operation::Python { requirements, r } => {
                let r = r.map(|r| r.on_host(&self.rscript));
                self.prepare_uv(r.as_ref(), on_started)?;
                let python = resolver::resolve_python_manifest(
                    requirements,
                    &self.python,
                    r.as_ref(),
                    on_started,
                )?;
                serde_json::to_value(python).map_err(|error| error.to_string())
            }
            Operation::PythonVersion { constraints, r } => {
                let r = r.on_host(&self.rscript);
                self.prepare_uv(Some(&r), on_started)?;
                resolver::resolve_python_version(constraints, &self.python, &r, on_started)
                    .map(serde_json::Value::String)
            }
            Operation::Duckdb { r, extensions } => {
                let r = r.on_host(&self.rscript);
                resolver::resolve_duckdb_extensions(&r, &extensions, on_started)?;
                Ok(serde_json::Value::Null)
            }
        }
    }

    fn prepare_uv(
        &mut self,
        r: Option<&resolver::ManagedR>,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(), String> {
        if !self.managed_python {
            return Err("managed Python requirements are disabled because the session uses a user-selected Python environment".into());
        }
        if !self.python.has_uv() {
            let r = r.ok_or("remote Python bootstrap requires managed R")?;
            let configuration = self
                .r
                .as_ref()
                .ok_or("remote R bootstrap has not been prepared")?;
            let uv = configuration.resolve_uv(r, &self.python, on_started)?;
            self.python.set_resolved_uv(uv);
        }
        Ok(())
    }
}

enum Event {
    Input(Result<Input, String>),
    Started(ResolverStopHandle, mpsc::Sender<()>),
    Completed(Output),
    OutputFailed(String),
}

fn perform<T>(
    id: u64,
    events: &mpsc::Sender<Event>,
    run: impl FnOnce(&dyn Fn(ResolverStopHandle) -> Result<(), String>) -> Result<T, String>,
    value: impl FnOnce(&T) -> serde_json::Value,
) -> Result<T, String> {
    let handles = Mutex::new(Vec::new());
    let result = run(&|handle| {
        handles
            .lock()
            .expect("preparation handles lock")
            .push(handle.clone());
        let (registered, response) = mpsc::channel();
        events
            .send(Event::Started(handle, registered))
            .map_err(|_| "preparation owner stopped")?;
        response
            .recv()
            .map_err(|_| "preparation owner stopped".to_string())
    });
    let handles = handles.into_inner().expect("preparation handles lock");
    let confirmed = handles.iter().all(ResolverStopHandle::cleanup_confirmed);
    let control = handles.iter().find_map(ResolverStopHandle::control_outcome);
    events
        .send(Event::Completed(Output::Completed {
            id,
            result: result.as_ref().map(value).map_err(Clone::clone),
            control,
            confirmed,
        }))
        .map_err(|_| "preparation owner stopped")?;
    result
}

pub(super) fn run() -> Result<(), String> {
    let mut input = Io::new(
        duplicate(0)?,
        None,
        Some(Instant::now() + crate::ssh::SETUP_TIMEOUT),
    )?;
    let Input::Open {
        version,
        build,
        workspace,
        selections,
    } = super::read(&mut input)?
    else {
        return Err("expected SSH preparation open".into());
    };
    if version != super::VERSION || build != env!("CARGO_PKG_VERSION") {
        return Err("incompatible SSH preparation protocol or Console build".into());
    }
    crate::ssh::enter_workspace(&workspace)?;
    // Only these runtime selections cross the workload boundary. This is the
    // single-threaded entry point; later worker environment changes cannot reach it.
    unsafe {
        if let Some(home) = selections.r_home {
            std::env::set_var("R_HOME", home);
        }
        if let Some(python) = selections.python {
            std::env::set_var("RETICULATE_PYTHON", python);
        }
    }
    let (events, received) = mpsc::channel();
    let (outgoing, output) = mpsc::channel();
    let (input_cancelled, input_cancel) = io::pipe().map_err(|e| e.to_string())?;
    let (output_cancelled, output_cancel) = io::pipe().map_err(|e| e.to_string())?;
    let input_events = events.clone();
    let input_task = thread::spawn(move || {
        let result = (|| {
            let mut input = Io::new(duplicate(0)?, Some(input_cancelled), None)?;
            loop {
                input_events
                    .send(Event::Input(Ok(super::read(&mut input)?)))
                    .map_err(|_| "preparation owner stopped")?;
            }
            #[allow(unreachable_code)]
            Ok::<(), String>(())
        })();
        if let Err(error) = result {
            let _ = input_events.send(Event::Input(Err(error)));
        }
    });
    let output_events = events.clone();
    let output_task = thread::spawn(move || {
        let result = (|| {
            let mut writer = Io::new(duplicate(1)?, Some(output_cancelled), None)?;
            for message in output {
                super::write(&mut writer, &message)?;
            }
            Ok::<(), String>(())
        })();
        if let Err(error) = result {
            let _ = output_events.send(Event::OutputFailed(error));
        }
    });
    outgoing
        .send(Output::Hello {
            version: super::VERSION,
            build: env!("CARGO_PKG_VERSION").into(),
        })
        .map_err(|_| "preparation output stopped")?;
    let (jobs, work) = mpsc::channel();
    let job_events = events.clone();
    let worker = thread::spawn(move || {
        let mut context = perform(0, &job_events, Context::discover, |(_, discovery)| {
            serde_json::to_value(discovery).expect("discovery serializes")
        })?
        .0;
        for (id, operation) in work {
            let _ = perform(
                id,
                &job_events,
                |started| context.execute(operation, started),
                Clone::clone,
            );
        }
        Ok::<(), String>(())
    });
    let mut active = Some(0);
    let mut last_id = 0;
    let mut handle: Option<ResolverStopHandle> = None;
    let mut pending: Option<ResolverControlOutcome> = None;
    let mut closing = false;
    let mut failure = None;
    let mut confirmed = true;
    while active.is_some() || !closing {
        let event = received
            .recv()
            .map_err(|_| "preparation owner events stopped")?;
        match event {
            Event::Input(Ok(Input::Run { id, operation }))
                if !closing && active.is_none() && id > last_id =>
            {
                active = Some(id);
                last_id = id;
                handle = None;
                pending = None;
                if jobs.send((id, operation)).is_err() {
                    failure = Some("remote preparation executor stopped".into());
                    closing = true;
                    active = None;
                }
            }
            Event::Input(Ok(Input::Control { id, control })) if active == Some(id) => {
                pending.get_or_insert(control);
                let result = if let Some(handle) = &handle {
                    apply(handle, control)
                } else {
                    Ok(true)
                };
                let _ = outgoing.send(Output::Controlled { id, result });
            }
            Event::Input(Ok(Input::Control { id, .. })) => {
                let _ = outgoing.send(Output::Controlled {
                    id,
                    result: Ok(false),
                });
            }
            Event::Started(started, registered) => {
                let _ = registered.send(());
                if closing {
                    let _ = started.stop();
                } else if let Some(control) = pending {
                    let _ = apply(&started, control);
                }
                handle = Some(started);
            }
            Event::Completed(message) => {
                if let Output::Completed {
                    confirmed: retired, ..
                } = &message
                {
                    confirmed &= retired;
                }
                active = None;
                handle = None;
                let _ = outgoing.send(message);
                if !confirmed {
                    failure = Some("remote preparation retirement is unconfirmed".into());
                    closing = true;
                }
            }
            Event::Input(Ok(Input::Close)) => closing = true,
            Event::Input(Err(error)) | Event::OutputFailed(error) => {
                failure = Some(error);
                closing = true;
            }
            _ => {
                failure = Some("unexpected SSH preparation request".into());
                closing = true;
            }
        }
        if closing && let Some(handle) = &handle {
            let _ = handle.stop();
        }
    }
    drop(jobs);
    let _ = worker
        .join()
        .map_err(|_| "remote preparation executor panicked")?;
    drop(input_cancel);
    let _ = input_task.join();
    if failure.is_none() && confirmed {
        let _ = outgoing.send(Output::Closed);
    } else {
        drop(output_cancel);
    }
    drop(outgoing);
    let _ = output_task.join();
    failure.map_or(Ok(()), Err)
}

fn apply(handle: &ResolverStopHandle, control: ResolverControlOutcome) -> Result<bool, String> {
    match control {
        ResolverControlOutcome::Interrupted => handle.interrupt(),
        ResolverControlOutcome::Cancelled => handle.stop().map(|()| true),
    }
}
