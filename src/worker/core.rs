use std::collections::VecDeque;
use std::io;
use std::os::fd::{AsRawFd, RawFd};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};

use crate::worker_protocol::{ConsoleChannel, ServerMessage, WorkerMessage};

static WORKER_READER: OnceLock<Mutex<crate::sideband::Reader>> = OnceLock::new();
static WORKER_WRITER: OnceLock<crate::sideband::Writer> = OnceLock::new();
static PENDING_SERVER_MESSAGES: Mutex<VecDeque<ServerMessage>> = Mutex::new(VecDeque::new());
static WORKER_FAILURE: Mutex<Option<String>> = Mutex::new(None);
static WORKER_SHUTDOWN: AtomicBool = AtomicBool::new(false);
static SQL_EVALUATION_STARTED: AtomicBool = AtomicBool::new(false);

pub(crate) fn initialize(
    reader: crate::sideband::Reader,
    writer: crate::sideband::Writer,
) -> io::Result<()> {
    WORKER_READER
        .set(Mutex::new(reader))
        .map_err(|_| io::Error::other("R worker sideband was already initialized"))?;
    WORKER_WRITER
        .set(writer)
        .map_err(|_| io::Error::other("R worker sideband was already initialized"))
}

pub(crate) fn sideband_activity() -> Result<(bool, RawFd), String> {
    let reader = worker_reader()?;
    Ok((reader.has_buffered_data(), reader.as_raw_fd()))
}

pub(crate) fn receive_server_message() -> Result<ServerMessage, String> {
    worker_reader()?
        .receive()
        .map_err(|error| format!("worker sideband read failed: {error}"))
}

pub(crate) fn take_pending_server_message() -> Result<Option<ServerMessage>, String> {
    PENDING_SERVER_MESSAGES
        .lock()
        .map_err(|_| "pending server message lock poisoned".to_string())
        .map(|mut messages| messages.pop_front())
}

pub(crate) fn is_shutting_down() -> bool {
    WORKER_SHUTDOWN.load(Ordering::SeqCst)
}

pub(crate) fn mark_shutting_down() {
    WORKER_SHUTDOWN.store(true, Ordering::SeqCst);
}

pub(crate) fn set_sql_evaluation_started(started: bool) {
    SQL_EVALUATION_STARTED.store(started, Ordering::SeqCst);
}

pub(crate) fn observe_stdin_shutdown() -> Result<(), String> {
    let mut event = libc::pollfd {
        fd: libc::STDIN_FILENO,
        events: libc::POLLIN,
        revents: 0,
    };
    loop {
        let result = unsafe { libc::poll(&mut event, 1, 0) };
        if result >= 0 {
            if event.revents & libc::POLLHUP != 0 {
                mark_shutting_down();
            }
            return Ok(());
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(format!("worker stdin readiness check failed: {error}"));
        }
    }
}

pub(crate) fn record_worker_failure(message: String) {
    let mut failure = WORKER_FAILURE
        .lock()
        .expect("R worker failure lock should not be poisoned");
    if failure.is_none() {
        *failure = Some(message);
    }
}

pub(crate) fn take_worker_failure() -> Option<String> {
    WORKER_FAILURE
        .lock()
        .expect("R worker failure lock should not be poisoned")
        .take()
}

pub(crate) fn emit_output(channel: ConsoleChannel, bytes: &[u8]) {
    if let Err(error) = send_output(channel, bytes) {
        record_worker_failure(error);
    }
}

pub(crate) fn send_input_requested(prompt: &str) -> Result<(), String> {
    worker_writer()
        .expect("R worker sideband writer should be initialized")
        .send(&WorkerMessage::InputRequested {
            prompt: prompt.to_string(),
        })
        .map_err(|error| format!("R worker failed to report an input request: {error}"))
}

pub(crate) fn send_input_received() -> Result<(), String> {
    worker_writer()
        .expect("R worker sideband writer should be initialized")
        .send(&WorkerMessage::InputReceived)
        .map_err(|error| format!("R worker failed to report received input: {error}"))
}

pub(crate) fn send_input_cancelled() -> Result<(), String> {
    worker_writer()
        .expect("R worker sideband writer should be initialized")
        .send(&WorkerMessage::InputCancelled)
        .map_err(|error| format!("R worker failed to cancel an input request: {error}"))
}

pub(crate) fn publish_plot(image: Result<String, String>) {
    if let Err(error) = image.and_then(send_image) {
        record_worker_failure(error);
    }
}

pub(crate) fn resolve_python(
    request: crate::worker_protocol::PythonResolveRequest,
) -> Result<String, String> {
    send_worker_message(&WorkerMessage::ResolvePython { request })?;
    match receive_resolver_message().map_err(infrastructure_failure)? {
        ServerMessage::PythonResolved { python } => {
            crate::python::link_matplotlib_caches();
            Ok(python)
        }
        ServerMessage::PythonResolutionFailed { message } => Err(message),
        ServerMessage::RResolved { .. } | ServerMessage::RResolutionFailed { .. } => {
            Err(infrastructure_failure(
                "worker received an R environment response while resolving Python".to_string(),
            ))
        }
        ServerMessage::PythonVersionResolved { .. }
        | ServerMessage::PythonVersionResolutionFailed { .. } => Err(infrastructure_failure(
            "worker received a Python version response while resolving Python".to_string(),
        )),
        ServerMessage::Shutdown => {
            mark_shutting_down();
            Err("worker is shutting down".to_string())
        }
        ServerMessage::Evaluate { .. } => Err(infrastructure_failure(
            "worker received an evaluation while resolving Python".to_string(),
        )),
        ServerMessage::PreparePython { .. } => Err(infrastructure_failure(
            "worker received Python preparation while resolving Python".to_string(),
        )),
        ServerMessage::PrepareR { .. } => Err(infrastructure_failure(
            "worker received R preparation while resolving Python".to_string(),
        )),
    }
}

pub(crate) fn publish_python_activation(
    requirements: crate::worker_protocol::PythonRequirementManifest,
) -> Result<(), String> {
    send_worker_message(&WorkerMessage::PythonActivated { requirements })
}

pub(crate) fn resolve_r(
    packages: Vec<String>,
) -> Result<crate::r_environment::ResolutionOutcome, String> {
    use crate::r_environment::{ResolutionFailureKind, ResolutionOutcome};
    use crate::worker_protocol::RResolutionFailureKind;

    if SQL_EVALUATION_STARTED.load(Ordering::SeqCst) {
        return Ok(ResolutionOutcome::Unavailable);
    }
    send_worker_message(&WorkerMessage::ResolveR { packages })?;
    match receive_resolver_message().map_err(infrastructure_failure)? {
        ServerMessage::RResolved { library } => Ok(ResolutionOutcome::Resolved { library }),
        ServerMessage::RResolutionFailed { failure, message } => {
            let failure = match failure {
                RResolutionFailureKind::Host => ResolutionFailureKind::Host,
                RResolutionFailureKind::Interrupted => ResolutionFailureKind::Interrupted,
                RResolutionFailureKind::Operation => {
                    return Err(infrastructure_failure(message));
                }
            };
            Ok(ResolutionOutcome::Failed { failure, message })
        }
        ServerMessage::Shutdown => {
            mark_shutting_down();
            Err("worker is shutting down".to_string())
        }
        ServerMessage::PythonResolved { .. }
        | ServerMessage::PythonResolutionFailed { .. }
        | ServerMessage::PythonVersionResolved { .. }
        | ServerMessage::PythonVersionResolutionFailed { .. } => Err(infrastructure_failure(
            "worker received a Python resolver response while resolving R".to_string(),
        )),
        ServerMessage::Evaluate { .. }
        | ServerMessage::PreparePython { .. }
        | ServerMessage::PrepareR { .. } => Err(infrastructure_failure(
            "worker received an operation while resolving R".to_string(),
        )),
    }
}

pub(crate) fn publish_r_activation(library: String) -> Result<(), String> {
    send_worker_message(&WorkerMessage::RActivated { library })
}

pub(crate) fn publish_r_activation_failure(library: String, message: String) -> Result<(), String> {
    send_worker_message(&WorkerMessage::RActivationFailed { library, message })
}

pub(crate) fn resolve_python_version(
    request: crate::worker_protocol::PythonVersionResolveRequest,
) -> Result<String, String> {
    send_worker_message(&WorkerMessage::ResolvePythonVersion { request })?;
    match receive_resolver_message().map_err(infrastructure_failure)? {
        ServerMessage::PythonVersionResolved { version } => Ok(version),
        ServerMessage::PythonVersionResolutionFailed { message } => Err(message),
        ServerMessage::Shutdown => {
            mark_shutting_down();
            Err("worker is shutting down".to_string())
        }
        ServerMessage::Evaluate { .. } => Err(infrastructure_failure(
            "worker received an evaluation while resolving a Python version".to_string(),
        )),
        ServerMessage::PreparePython { .. } => Err(infrastructure_failure(
            "worker received Python preparation while resolving a Python version".to_string(),
        )),
        ServerMessage::PrepareR { .. } => Err(infrastructure_failure(
            "worker received R preparation while resolving a Python version".to_string(),
        )),
        ServerMessage::RResolved { .. } | ServerMessage::RResolutionFailed { .. } => {
            Err(infrastructure_failure(
                "worker received an R environment response while resolving a Python version"
                    .to_string(),
            ))
        }
        ServerMessage::PythonResolved { .. } | ServerMessage::PythonResolutionFailed { .. } => {
            Err(infrastructure_failure(
                "worker received a Python environment response while resolving a Python version"
                    .to_string(),
            ))
        }
    }
}

fn receive_resolver_message() -> Result<ServerMessage, String> {
    loop {
        let message = receive_server_message()?;
        match message {
            ServerMessage::Evaluate { .. }
            | ServerMessage::PreparePython { .. }
            | ServerMessage::PrepareR { .. } => queue_server_message(message)?,
            _ => return Ok(message),
        }
    }
}

fn queue_server_message(message: ServerMessage) -> Result<(), String> {
    PENDING_SERVER_MESSAGES
        .lock()
        .map_err(|_| "pending server message lock poisoned".to_string())?
        .push_back(message);
    Ok(())
}

fn worker_reader() -> Result<std::sync::MutexGuard<'static, crate::sideband::Reader>, String> {
    WORKER_READER
        .get()
        .ok_or_else(|| "R worker sideband reader is not initialized".to_string())?
        .lock()
        .map_err(|_| "R worker sideband reader lock poisoned".to_string())
}

fn worker_writer() -> Option<&'static crate::sideband::Writer> {
    WORKER_WRITER.get()
}

fn send_worker_message(message: &WorkerMessage) -> Result<(), String> {
    if !crate::sideband::available_in_process() {
        return Err("managed environment resolution is unavailable in a fork child".to_string());
    }
    worker_writer()
        .ok_or_else(|| "R worker sideband writer is not initialized".to_string())?
        .send(message)
        .map_err(|error| format!("worker sideband write failed: {error}"))
        .map_err(infrastructure_failure)
}

fn infrastructure_failure(message: String) -> String {
    record_worker_failure(message.clone());
    message
}

fn send_output(channel: ConsoleChannel, bytes: &[u8]) -> Result<(), String> {
    if !crate::sideband::available_in_process() {
        return Ok(());
    }
    let Some(writer) = worker_writer() else {
        return Ok(());
    };
    if WORKER_FAILURE.lock().is_ok_and(|failure| failure.is_some()) {
        return Ok(());
    }
    let data = String::from_utf8_lossy(bytes).into_owned();
    let message = match channel {
        ConsoleChannel::Output => WorkerMessage::ConsoleOutput { data },
        ConsoleChannel::Diagnostic => WorkerMessage::ConsoleDiagnostic { data },
    };
    writer
        .send(&message)
        .map_err(|error| format!("R console output failed: {error}"))
}

fn send_image(data: String) -> Result<(), String> {
    worker_writer()
        .expect("R worker sideband writer should be initialized")
        .send(&WorkerMessage::Image {
            data,
            mime_type: "image/png".to_string(),
        })
        .map_err(|error| format!("R worker failed to send a plot image: {error}"))
}
