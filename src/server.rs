use std::error::Error;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};

use rmcp::{
    ServerHandler, ServiceExt, handler::server::wrapper::Parameters, schemars, tool, tool_handler,
    tool_router,
};
use serde::Deserialize;
use tokio::io::{AsyncRead, ReadBuf};

#[derive(Clone)]
struct ConsoleServer {
    worker: Arc<Mutex<WorkerState>>,
}

enum WorkerState {
    Cold,
    Idle(crate::worker::RWorker),
    Running(Option<crate::worker::WorkerControl>),
    InputRequired(crate::worker::RWorker),
    Stopped(String),
}

#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct ConsoleArguments {
    /// Complete multiline R code evaluated as top-level expressions.
    r: Option<String>,
    /// Exact text supplied after R requests interactive input.
    stdin: Option<String>,
}

enum ConsoleOperation {
    Evaluate(String),
    Input(String),
}

impl ConsoleServer {
    fn new() -> Self {
        Self {
            worker: Arc::new(Mutex::new(WorkerState::Cold)),
        }
    }
}

#[tool_router]
impl ConsoleServer {
    #[tool(
        description = "Persistent R console. Send one complete r cell, or send stdin after the response ends with [input]."
    )]
    async fn send(
        &self,
        Parameters(ConsoleArguments { r, stdin }): Parameters<ConsoleArguments>,
    ) -> Result<String, String> {
        let operation = match (r, stdin) {
            (Some(r), None) => ConsoleOperation::Evaluate(r),
            (None, Some(stdin)) => ConsoleOperation::Input(stdin),
            _ => return Err("send exactly one of r or stdin".to_string()),
        };
        let worker = self.worker.clone();
        tokio::task::spawn_blocking(move || run_operation(&worker, operation))
            .await
            .map_err(|error| format!("R worker task failed: {error}"))?
    }
}

fn run_operation(
    shared: &Mutex<WorkerState>,
    operation: ConsoleOperation,
) -> Result<String, String> {
    let worker = {
        let mut state = shared
            .lock()
            .map_err(|_| "R worker state lock poisoned".to_string())?;
        match (
            &operation,
            std::mem::replace(&mut *state, WorkerState::Running(None)),
        ) {
            (ConsoleOperation::Evaluate(_), WorkerState::Cold) => None,
            (ConsoleOperation::Evaluate(_), WorkerState::Idle(worker)) => {
                *state = WorkerState::Running(Some(worker.control()));
                Some(worker)
            }
            (ConsoleOperation::Evaluate(_), WorkerState::Running(control)) => {
                *state = WorkerState::Running(control);
                return Err("cannot evaluate R code while the session is running".to_string());
            }
            (ConsoleOperation::Evaluate(_), WorkerState::InputRequired(worker)) => {
                *state = WorkerState::InputRequired(worker);
                return Err(
                    "cannot evaluate R code while the session is waiting for stdin".to_string(),
                );
            }
            (_, WorkerState::Cold) => {
                *state = WorkerState::Cold;
                return Err("stdin is accepted only at an R input prompt".to_string());
            }
            (_, WorkerState::Idle(worker)) => {
                *state = WorkerState::Idle(worker);
                return Err("stdin is accepted only at an R input prompt".to_string());
            }
            (ConsoleOperation::Input(_), WorkerState::Running(control)) => {
                *state = WorkerState::Running(control);
                return Err("stdin is accepted only at an R input prompt".to_string());
            }
            (ConsoleOperation::Input(_), WorkerState::InputRequired(worker)) => {
                *state = WorkerState::Running(Some(worker.control()));
                Some(worker)
            }
            (_, WorkerState::Stopped(message)) => {
                *state = WorkerState::Stopped(message.clone());
                return Err(message);
            }
        }
    };

    let mut worker = match worker {
        Some(worker) => worker,
        None => match crate::worker::RWorker::start(|control| {
            if let Ok(mut state) = shared.lock() {
                *state = WorkerState::Running(Some(control));
            }
        }) {
            Ok(worker) => worker,
            Err(error) => {
                let message = stopped_message(error.to_string());
                *shared
                    .lock()
                    .map_err(|_| "R worker state lock poisoned".to_string())? =
                    WorkerState::Stopped(message.clone());
                return Err(message);
            }
        },
    };

    let result = match operation {
        ConsoleOperation::Evaluate(r) => worker.evaluate(r),
        ConsoleOperation::Input(stdin) => {
            if stdin.contains('\0') {
                let mut state = shared
                    .lock()
                    .map_err(|_| "R worker state lock poisoned".to_string())?;
                *state = WorkerState::InputRequired(worker);
                return Err("stdin cannot contain NUL".to_string());
            }
            worker.provide_input(stdin)
        }
    };

    let mut state = shared
        .lock()
        .map_err(|_| "R worker state lock poisoned".to_string())?;
    match result {
        Ok(crate::worker::Boundary::Complete(output)) => {
            *state = WorkerState::Idle(worker);
            Ok(output)
        }
        Ok(crate::worker::Boundary::Input(output)) => {
            *state = WorkerState::InputRequired(worker);
            Ok(output)
        }
        Err(error) => {
            let message = stopped_message(error);
            drop(worker);
            *state = WorkerState::Stopped(message.clone());
            Err(message)
        }
    }
}

fn stopped_message(message: String) -> String {
    format!("[stopped: {message}]")
}

fn shutdown_worker(shared: &Mutex<WorkerState>) -> bool {
    let control = {
        let Ok(state) = shared.lock() else {
            return true;
        };
        match &*state {
            WorkerState::Cold | WorkerState::Stopped(_) => return true,
            WorkerState::Running(None) => return false,
            WorkerState::Idle(worker) | WorkerState::InputRequired(worker) => {
                Some(worker.control())
            }
            WorkerState::Running(Some(control)) => Some(control.clone()),
        }
    };
    if let Some(control) = control {
        control.shutdown();
    }
    true
}

struct ShutdownReader<R> {
    inner: R,
    closed: Arc<AtomicBool>,
}

impl<R> ShutdownReader<R> {
    fn new(inner: R, closed: Arc<AtomicBool>) -> Self {
        Self { inner, closed }
    }
}

impl<R: AsyncRead + Unpin> AsyncRead for ShutdownReader<R> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let filled = buffer.filled().len();
        match Pin::new(&mut self.inner).poll_read(context, buffer) {
            Poll::Ready(Ok(())) if buffer.filled().len() == filled => {
                self.closed.store(true, Ordering::SeqCst);
                Poll::Ready(Ok(()))
            }
            Poll::Ready(Err(error)) => {
                self.closed.store(true, Ordering::SeqCst);
                Poll::Ready(Err(error))
            }
            poll => poll,
        }
    }
}

#[tool_handler(name = "mcp-console")]
impl ServerHandler for ConsoleServer {}

pub async fn run() -> Result<(), Box<dyn Error>> {
    let server = ConsoleServer::new();
    let worker = server.worker.clone();
    let input_closed = Arc::new(AtomicBool::new(false));
    let input = ShutdownReader::new(tokio::io::stdin(), input_closed.clone());
    let service = server.serve((input, tokio::io::stdout())).await?;
    let monitor_input_closed = input_closed.clone();
    let monitor = std::thread::spawn(move || {
        loop {
            if monitor_input_closed.load(Ordering::SeqCst) && shutdown_worker(&worker) {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(25));
        }
    });
    let result = service.waiting().await;
    input_closed.store(true, Ordering::SeqCst);
    let _ = monitor.join();
    result?;
    Ok(())
}
