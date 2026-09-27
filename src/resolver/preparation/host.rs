use std::io;
use std::sync::{Mutex, mpsc};
use std::thread;
use std::time::Instant;

use super::{Input, Output};
use crate::resolver::{ResolverControlOutcome, ResolverStopHandle};
use crate::target_launch::transfer::{Io, duplicate};

enum Context {
    Local(Box<crate::resolver::broker::Context>),
    Remote {
        launch: Option<crate::resolver::broker::Launch>,
        connection: Option<super::Preparation>,
    },
}

impl Context {
    fn discover(
        &mut self,
        started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<super::Discovery, String> {
        match self {
            Self::Local(context) => context.discover(started),
            Self::Remote { launch, connection } => {
                let (broker, discovery) = super::Preparation::local(
                    launch.take().expect("captured remote launch"),
                    started,
                )?;
                *connection = Some(broker);
                Ok(discovery)
            }
        }
    }
    fn execute(
        &mut self,
        operation: super::Operation,
        started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<serde_json::Value, String> {
        match self {
            Self::Local(context) => context.execute(operation, started),
            Self::Remote { connection, .. } => connection
                .as_ref()
                .expect("remote broker")
                .call(operation, started),
        }
    }
    fn release(self) -> Result<(), String> {
        match self {
            Self::Local(context) => context.release(),
            Self::Remote { connection, .. } => connection.map_or(Ok(()), |broker| broker.close()),
        }
    }
    fn quarantine(self) -> Result<(), String> {
        match self {
            Self::Local(context) => context.quarantine(),
            Self::Remote { connection, .. } => {
                connection.map_or(Ok(()), |broker| broker.quarantine())
            }
        }
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
            result: Some(result.as_ref().map(value).map_err(Clone::clone)),
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
        Some(Instant::now() + super::SETUP_TIMEOUT),
    )?;
    let Input::Open {
        version,
        build,
        workspace,
        selections,
        launch,
        no_sandbox,
        settings,
    } = super::read(&mut input)?
    else {
        return Err("expected resolver preparation open".into());
    };
    if version != super::VERSION || build != env!("CARGO_PKG_VERSION") {
        return Err("incompatible resolver preparation protocol or Console build".into());
    }
    crate::target_launch::enter_workspace(&workspace)?;
    let remote = launch.is_none();
    let mut settings = settings;
    for (name, value) in [
        ("R_HOME", selections.r_home),
        ("RETICULATE_PYTHON", selections.python),
    ] {
        if let Some(value) = value {
            if value.contains('\0') {
                return Err(format!("remote {name} selection must not contain NUL"));
            }
            settings.environment.insert(name.into(), value);
        }
    }
    let launch = match launch {
        Some(launch) => *launch,
        None => crate::resolver::broker::Launch::capture(no_sandbox, settings)?,
    };
    let context = if remote {
        Context::Remote {
            launch: Some(launch),
            connection: None,
        }
    } else {
        Context::Local(Box::new(crate::resolver::broker::Context::new(launch)?))
    };
    let (events, received) = mpsc::channel();
    let (outgoing, output) = mpsc::channel::<Output>();
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
                message.write(&mut writer)?;
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
        let mut context = context;
        let _ = perform(
            0,
            &job_events,
            |started| context.discover(started),
            |discovery| serde_json::to_value(discovery).expect("discovery serializes"),
        );
        for (id, operation) in work {
            let _ = perform(
                id,
                &job_events,
                |started| context.execute(operation, started),
                Clone::clone,
            );
        }
        Ok::<_, String>(context)
    });
    let mut active = Some(0);
    let mut last_id = 0;
    let mut handle: Option<ResolverStopHandle> = None;
    let mut pending: Option<ResolverControlOutcome> = None;
    let mut closing = false;
    let mut release = None;
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
                    failure = Some("resolver preparation executor stopped".into());
                    closing = true;
                    active = None;
                }
            }
            Event::Input(Ok(Input::Control { id, control })) if active == Some(id) => {
                pending.get_or_insert(control);
                let result = if let Some(handle) = &handle {
                    // The operation still owns this control between resolver
                    // stages, even after the previous handle has completed.
                    apply(handle, control).map(|_| true)
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
                    failure = Some("resolver preparation retirement is unconfirmed".into());
                    closing = true;
                }
            }
            Event::Input(Ok(Input::Close { release: requested })) => {
                closing = true;
                release = Some(requested);
            }
            Event::Input(Err(error)) | Event::OutputFailed(error) => {
                failure = Some(error);
                closing = true;
            }
            _ => {
                failure = Some("unexpected resolver preparation request".into());
                closing = true;
            }
        }
        if closing && let Some(handle) = &handle {
            let _ = handle.stop();
        }
    }
    drop(jobs);
    let context = worker
        .join()
        .map_err(|_| "resolver preparation executor panicked")??;
    drop(input_cancel);
    let _ = input_task.join();
    if !confirmed || release == Some(false) {
        context.quarantine()?;
    } else {
        // On owner loss, retained native launchers still hold their own locks.
        context.release()?;
    }
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
