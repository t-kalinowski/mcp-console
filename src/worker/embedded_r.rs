use std::collections::VecDeque;
use std::error::Error;
use std::ffi::{CStr, CString, c_char, c_int, c_uchar, c_void};
use std::io;
use std::os::fd::AsRawFd;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use std::thread;

use crate::cell::{Cell, Language};
use crate::worker_protocol::{ConsoleChannel, ServerMessage, WorkerMessage};

static R_MAIN_ARGS: OnceLock<Vec<CString>> = OnceLock::new();
static WORKER_READER: OnceLock<Mutex<crate::sideband::Reader>> = OnceLock::new();
static WORKER_WRITER: OnceLock<crate::sideband::Writer> = OnceLock::new();
static R_REPL_INIT: OnceLock<ReplInit> = OnceLock::new();
static R_REPL_DO_ONE: OnceLock<ReplDoOne> = OnceLock::new();
static R_EVENTS: OnceLock<REvents> = OnceLock::new();
static R_CHECK_USER_INTERRUPT: OnceLock<CheckUserInterrupt> = OnceLock::new();
static CELL_SOURCE: Mutex<Option<CellSource>> = Mutex::new(None);
static CONSOLE_STDIN: Mutex<ConsoleStdin> = Mutex::new(ConsoleStdin {
    pushback: VecDeque::new(),
    line_prefix: Vec::new(),
});
static PENDING_SERVER_MESSAGES: Mutex<VecDeque<ServerMessage>> = Mutex::new(VecDeque::new());
static WORKER_FAILURE: Mutex<Option<String>> = Mutex::new(None);
static WORKER_SHUTDOWN: AtomicBool = AtomicBool::new(false);
static EVALUATION_STARTED: AtomicBool = AtomicBool::new(false);
type ReplInit = unsafe extern "C-unwind" fn();
type ReplDoOne = unsafe extern "C-unwind" fn() -> c_int;
type TopLevelExec = unsafe extern "C-unwind" fn(
    Option<unsafe extern "C-unwind" fn(*mut c_void)>,
    *mut c_void,
) -> c_int;
type CheckActivity = unsafe extern "C-unwind" fn(c_int, c_int) -> *mut c_void;
type RunHandlers = unsafe extern "C-unwind" fn(*mut c_void, *mut c_void);
type ReadConsole = unsafe extern "C-unwind" fn(
    prompt: *const c_char,
    buffer: *mut c_uchar,
    length: c_int,
    add_history: c_int,
) -> c_int;
type CheckUserInterrupt = unsafe extern "C-unwind" fn();
type AddInputHandler = unsafe extern "C-unwind" fn(
    *mut c_void,
    c_int,
    Option<unsafe extern "C-unwind" fn(*mut c_void)>,
    c_int,
) -> *mut c_void;
type RemoveInputHandler = unsafe extern "C-unwind" fn(*mut *mut c_void, *mut c_void) -> c_int;

struct CellSource {
    text: String,
    offset: usize,
}

struct ConsoleStdin {
    pushback: VecDeque<ConsoleStdinChunk>,
    line_prefix: Vec<u8>,
}

struct ConsoleStdinChunk {
    bytes: Vec<u8>,
    offset: usize,
}

impl ConsoleStdin {
    unsafe fn copy_pushback(&mut self, destination: *mut u8, capacity: usize) -> usize {
        let mut copied = 0;
        while copied < capacity {
            let Some(chunk) = self.pushback.front_mut() else {
                break;
            };
            debug_assert!(chunk.offset <= chunk.bytes.len());
            let remaining = &chunk.bytes[chunk.offset..];
            let length = remaining.len().min(capacity - copied);
            unsafe {
                std::ptr::copy_nonoverlapping(remaining.as_ptr(), destination.add(copied), length);
            }
            copied += length;
            chunk.offset += length;
            if chunk.offset == chunk.bytes.len() {
                self.pushback.pop_front();
            }
        }
        if self.pushback.is_empty() {
            self.pushback = VecDeque::new();
        }
        copied
    }

    fn record_chunk(&mut self, chunk: &[u8]) {
        if chunk.last() == Some(&b'\n') {
            self.line_prefix = Vec::new();
        } else {
            self.line_prefix.extend_from_slice(chunk);
        }
    }

    fn preserve_line(&mut self, chunk: &[u8]) {
        if self.line_prefix.is_empty() && chunk.is_empty() {
            return;
        }
        if !chunk.is_empty() {
            self.pushback.push_front(ConsoleStdinChunk {
                bytes: chunk.to_vec(),
                offset: 0,
            });
        }
        if !self.line_prefix.is_empty() {
            self.pushback.push_front(ConsoleStdinChunk {
                bytes: std::mem::take(&mut self.line_prefix),
                offset: 0,
            });
        }
    }

    fn finish_operation(&mut self) {
        // A later callback cannot be assumed to continue this operation.
        self.preserve_line(&[]);
    }
}

struct REvents {
    top_level_exec: TopLevelExec,
    check_activity: CheckActivity,
    run_handlers: RunHandlers,
    add_input_handler: AddInputHandler,
    remove_input_handler: RemoveInputHandler,
    rg_wait_usec: usize,
}

struct Runtime {
    writer: crate::sideband::Writer,
    graphics: crate::r_graphics::Bridge,
    r_environment: crate::r_environment::Bridge,
    python: crate::python::Runtime,
    sql: crate::sql::Bridge,
}

unsafe extern "C" {
    fn mcp_r_run_ready_handlers(
        top_level_exec: TopLevelExec,
        check_activity: CheckActivity,
        run_handlers: RunHandlers,
        input_handlers: *mut c_void,
    );
    fn mcp_r_wait_for_activity(
        top_level_exec: TopLevelExec,
        add_input_handler: AddInputHandler,
        remove_input_handler: RemoveInputHandler,
        check_activity: CheckActivity,
        input_handlers: *mut *mut c_void,
        sideband_fd: c_int,
        wait_usec: c_int,
    ) -> c_int;
    fn mcp_r_repl_run_cell(
        init: ReplInit,
        do_one: ReplDoOne,
        before_do_one: extern "C" fn(),
        check_interrupt: CheckUserInterrupt,
        interrupts_pending: *const c_int,
    ) -> c_int;
}

unsafe extern "C-unwind" {
    fn mcp_r_console_configure(
        read_console: ReadConsole,
        check_interrupt: CheckUserInterrupt,
        interrupts_pending: *const c_int,
    );
    fn mcp_r_read_console(
        prompt: *const c_char,
        buffer: *mut c_uchar,
        length: c_int,
        add_history: c_int,
    ) -> c_int;
}

pub(crate) fn run() -> Result<(), Box<dyn Error>> {
    // SAFETY: pthread_main_np has no preconditions.
    if unsafe { libc::pthread_main_np() } != 1 {
        return Err(io::Error::other("R worker must run on the process main thread").into());
    }
    crate::python::configure_worker_environment()?;
    let (reader, writer) = crate::sideband::connect_from_env()?;
    let r_home = harp::command::r_home_setup()?;
    normalize_interrupt_signal()?;
    initialize_r(&r_home)?;
    WORKER_READER
        .set(Mutex::new(reader))
        .map_err(|_| io::Error::other("R worker sideband was already initialized"))?;
    WORKER_WRITER
        .set(writer.clone())
        .map_err(|_| io::Error::other("R worker sideband was already initialized"))?;
    let graphics = crate::r_graphics::Bridge::initialize()?;
    let r_environment = crate::r_environment::Bridge::initialize()?;
    let python = crate::python::Runtime::initialize()?;
    let sql = crate::sql::Bridge::initialize()?;
    writer.send(&WorkerMessage::Ready)?;

    Runtime {
        writer,
        graphics,
        r_environment,
        python,
        sql,
    }
    .run()
}

impl Runtime {
    fn run(&mut self) -> Result<(), Box<dyn Error>> {
        loop {
            if !self.handle(self.wait_for_message()?)? {
                return Ok(());
            }
        }
    }

    fn wait_for_message(&self) -> Result<ServerMessage, String> {
        loop {
            if WORKER_SHUTDOWN.load(Ordering::SeqCst) {
                return Ok(ServerMessage::Shutdown);
            }
            if let Some(message) = take_pending_server_message()? {
                return Ok(message);
            }

            let (buffered, sideband_fd) = {
                let reader = worker_reader()?;
                (reader.has_buffered_data(), reader.as_raw_fd())
            };
            if buffered {
                return receive_sideband_message();
            }
            if wait_for_activity(sideband_fd)? {
                return receive_sideband_message();
            }

            run_ready_handlers(&self.graphics)?;
            if let Some(message) = take_worker_failure() {
                return Err(message);
            }
        }
    }

    fn handle(&mut self, message: ServerMessage) -> Result<bool, Box<dyn Error>> {
        if matches!(
            &message,
            ServerMessage::PreparePython { .. } | ServerMessage::PrepareR { .. }
        ) {
            run_ready_handlers(&self.graphics).map_err(io::Error::other)?;
            if WORKER_SHUTDOWN.load(Ordering::SeqCst) {
                return Ok(false);
            }
            if let Some(message) = take_worker_failure() {
                return Err(io::Error::other(message).into());
            }
        }

        match message {
            ServerMessage::Evaluate { language, source } => {
                check_interrupts();
                let result = evaluate_cell(
                    Cell { language, source },
                    &self.graphics,
                    &mut self.python,
                    &mut self.sql,
                );
                check_interrupts();

                if WORKER_SHUTDOWN.load(Ordering::SeqCst) {
                    return Ok(false);
                }
                if let Some(message) = take_worker_failure().or_else(|| result.err()) {
                    return Err(io::Error::other(message).into());
                }
                self.writer.send(&WorkerMessage::Completed)?;
            }
            // Keep worker-owned preparation state transitions atomic. Any
            // nested host resolver registers its own interrupt target.
            ServerMessage::PreparePython { packages } => {
                let result = defer_interrupts(|| self.python.prepare(packages), discard_interrupts);
                if WORKER_SHUTDOWN.load(Ordering::SeqCst) {
                    return Ok(false);
                }
                if let Some(message) = take_worker_failure() {
                    return Err(io::Error::other(message).into());
                }
                match result {
                    Ok(crate::python::PreparationOutcome::Prepared) => {
                        self.writer.send(&WorkerMessage::PythonPrepared)?;
                    }
                    Ok(crate::python::PreparationOutcome::Failed { message }) => {
                        self.writer
                            .send(&WorkerMessage::PythonPreparationFailed { message })?;
                    }
                    Err(message) => return Err(io::Error::other(message).into()),
                }
            }
            ServerMessage::PrepareR { library } => {
                let result = defer_interrupts(
                    || self.r_environment.prepare(std::path::Path::new(&library)),
                    discard_interrupts,
                );
                if WORKER_SHUTDOWN.load(Ordering::SeqCst) {
                    return Ok(false);
                }
                if let Some(message) = take_worker_failure() {
                    return Err(io::Error::other(message).into());
                }
                match result.map_err(io::Error::other)? {
                    crate::r_environment::PreparationOutcome::Prepared { library } => {
                        self.writer.send(&WorkerMessage::RPrepared { library })?;
                    }
                    crate::r_environment::PreparationOutcome::Failed { message } => {
                        self.writer
                            .send(&WorkerMessage::RPreparationFailed { message })?;
                    }
                }
            }
            ServerMessage::Shutdown => return Ok(false),
            ServerMessage::RResolved { .. }
            | ServerMessage::RResolutionFailed { .. }
            | ServerMessage::PythonResolved { .. }
            | ServerMessage::PythonResolutionFailed { .. }
            | ServerMessage::PythonVersionResolved { .. }
            | ServerMessage::PythonVersionResolutionFailed { .. } => {
                return Err(
                    io::Error::other("worker received an unexpected resolver response").into(),
                );
            }
        }
        Ok(true)
    }
}

fn normalize_interrupt_signal() -> io::Result<()> {
    if unsafe { libc::signal(libc::SIGINT, libc::SIG_DFL) } == libc::SIG_ERR {
        return Err(io::Error::last_os_error());
    }
    let mut signals = unsafe { std::mem::zeroed() };
    if unsafe { libc::sigemptyset(&mut signals) } != 0
        || unsafe { libc::sigaddset(&mut signals, libc::SIGINT) } != 0
    {
        return Err(io::Error::last_os_error());
    }
    let result =
        unsafe { libc::pthread_sigmask(libc::SIG_UNBLOCK, &signals, std::ptr::null_mut()) };
    (result == 0)
        .then_some(())
        .ok_or_else(|| io::Error::from_raw_os_error(result))
}

fn check_interrupts() {
    if !interrupt_pending() {
        return;
    }
    let check = *R_CHECK_USER_INTERRUPT
        .get()
        .expect("R interrupt checker should be initialized");
    let _ = harp::top_level_exec(|| unsafe { check() });
}

fn defer_interrupts<T>(
    operation: impl FnOnce() -> Result<T, String>,
    after: impl FnOnce(),
) -> Result<T, String> {
    let previous = unsafe { libr::get(libr::R_interrupts_suspended) };
    unsafe { libr::set(libr::R_interrupts_suspended, libr::Rboolean_TRUE) };
    let result = operation();
    unsafe { libr::set(libr::R_interrupts_suspended, previous) };
    after();
    result
}

fn discard_interrupts() {
    unsafe { libr::set(libr::R_interrupts_pending, 0) };
}

fn interrupt_pending() -> bool {
    unsafe { libr::get(libr::R_interrupts_pending) != 0 }
}

fn console_interrupt_pending() -> bool {
    interrupt_pending()
        && unsafe { libr::get(libr::R_interrupts_suspended) == libr::Rboolean_FALSE }
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
            WORKER_SHUTDOWN.store(true, Ordering::SeqCst);
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
            WORKER_SHUTDOWN.store(true, Ordering::SeqCst);
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
            WORKER_SHUTDOWN.store(true, Ordering::SeqCst);
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
        let message = receive_sideband_message()?;
        match message {
            ServerMessage::Evaluate { .. }
            | ServerMessage::PreparePython { .. }
            | ServerMessage::PrepareR { .. } => queue_server_message(message)?,
            _ => return Ok(message),
        }
    }
}

fn receive_sideband_message() -> Result<ServerMessage, String> {
    worker_reader()?
        .receive()
        .map_err(|error| format!("worker sideband read failed: {error}"))
}

fn worker_reader() -> Result<std::sync::MutexGuard<'static, crate::sideband::Reader>, String> {
    WORKER_READER
        .get()
        .ok_or_else(|| "R worker sideband reader is not initialized".to_string())?
        .lock()
        .map_err(|_| "R worker sideband reader lock poisoned".to_string())
}

fn take_pending_server_message() -> Result<Option<ServerMessage>, String> {
    PENDING_SERVER_MESSAGES
        .lock()
        .map_err(|_| "pending server message lock poisoned".to_string())
        .map(|mut messages| messages.pop_front())
}

fn queue_server_message(message: ServerMessage) -> Result<(), String> {
    PENDING_SERVER_MESSAGES
        .lock()
        .map_err(|_| "pending server message lock poisoned".to_string())?
        .push_back(message);
    Ok(())
}

fn observe_stdin_shutdown() -> Result<(), String> {
    let mut event = libc::pollfd {
        fd: libc::STDIN_FILENO,
        events: libc::POLLIN,
        revents: 0,
    };
    loop {
        let result = unsafe { libc::poll(&mut event, 1, 0) };
        if result >= 0 {
            if event.revents & libc::POLLHUP != 0 {
                WORKER_SHUTDOWN.store(true, Ordering::SeqCst);
            }
            return Ok(());
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(format!("worker stdin readiness check failed: {error}"));
        }
    }
}

fn send_worker_message(message: &WorkerMessage) -> Result<(), String> {
    if !crate::sideband::available_in_process() {
        return Err("managed environment resolution is unavailable in a fork child".to_string());
    }
    WORKER_WRITER
        .get()
        .ok_or_else(|| "R worker sideband writer is not initialized".to_string())?
        .send(message)
        .map_err(|error| format!("worker sideband write failed: {error}"))
        .map_err(infrastructure_failure)
}

fn infrastructure_failure(message: String) -> String {
    record_worker_failure(message.clone());
    message
}

fn evaluate_cell(
    cell: Cell,
    graphics: &crate::r_graphics::Bridge,
    python: &mut crate::python::Runtime,
    sql: &mut crate::sql::Bridge,
) -> Result<(), String> {
    run_ready_handlers(graphics)?;
    if WORKER_SHUTDOWN.load(Ordering::SeqCst) {
        return Ok(());
    }
    if let Some(message) = take_worker_failure() {
        return Err(message);
    }
    let result = match cell.language {
        Language::R => evaluate_r_cell(cell.source, graphics),
        Language::Python => evaluate_python_cell(cell.source, graphics, python),
        Language::Sql => evaluate_sql_cell(cell.source, sql),
    };
    finish_console_stdin_operation()?;
    if result.is_ok() && !WORKER_SHUTDOWN.load(Ordering::SeqCst) {
        if let Some(message) = take_worker_failure() {
            return Err(message);
        }
        run_ready_handlers(graphics)?;
    }
    result
}

fn evaluate_r_cell(r: String, graphics: &crate::r_graphics::Bridge) -> Result<(), String> {
    if r.contains('\0') {
        emit_output(
            ConsoleChannel::Diagnostic,
            b"Error: R source cannot contain NUL\n",
        );
        return Ok(());
    }

    defer_interrupts(|| graphics.begin(), check_interrupts)?;
    set_cell_source(r);
    let status = run_repl_cell();
    clear_cell_source();
    let result = match status {
        0 | 1 => Ok(()),
        2 => {
            emit_output(ConsoleChannel::Diagnostic, b"Error: Incomplete code\n");
            Ok(())
        }
        status => Err(format!(
            "R worker received unexpected DLL REPL status {status}"
        )),
    };
    defer_interrupts(|| graphics.finish(), check_interrupts)?;
    result
}

fn evaluate_python_cell(
    source: String,
    graphics: &crate::r_graphics::Bridge,
    python: &mut crate::python::Runtime,
) -> Result<(), String> {
    if source.contains('\0') {
        emit_output(
            ConsoleChannel::Diagnostic,
            b"SyntaxError: source code string cannot contain null bytes\n",
        );
        return Ok(());
    }
    defer_interrupts(|| graphics.begin(), check_interrupts)?;
    EVALUATION_STARTED.store(true, Ordering::SeqCst);
    let result = python.evaluate(&source);
    EVALUATION_STARTED.store(false, Ordering::SeqCst);
    defer_interrupts(|| graphics.finish(), check_interrupts)?;
    result
}

fn evaluate_sql_cell(source: String, sql: &mut crate::sql::Bridge) -> Result<(), String> {
    if source.contains('\0') {
        emit_output(
            ConsoleChannel::Diagnostic,
            b"Error: SQL source cannot contain NUL\n",
        );
        return Ok(());
    }
    EVALUATION_STARTED.store(true, Ordering::SeqCst);
    let result = sql.evaluate(&source);
    EVALUATION_STARTED.store(false, Ordering::SeqCst);
    result
}

fn initialize_r(r_home: &std::path::Path) -> Result<(), Box<dyn Error>> {
    let libraries = harp::library::RLibraries::from_r_home_path(r_home);
    libraries.initialize_pre_setup_r();

    let arguments = ["mcp-console", "--quiet", "--interactive", "--vanilla"]
        .into_iter()
        .map(CString::new)
        .collect::<Result<Vec<_>, _>>()?;
    R_MAIN_ARGS
        .set(arguments)
        .map_err(|_| io::Error::other("R arguments were already initialized"))?;
    let mut argument_pointers = R_MAIN_ARGS
        .get()
        .expect("R arguments should be initialized")
        .iter()
        .map(|argument| argument.as_ptr() as *mut c_char)
        .collect::<Vec<_>>();

    unsafe {
        libr::Rf_initialize_R(
            argument_pointers.len() as c_int,
            argument_pointers.as_mut_ptr(),
        );
        libr::set(libr::R_Interactive, libr::Rboolean_TRUE);
        libr::set(libr::R_Consolefile, std::ptr::null_mut());
        libr::set(libr::R_Outputfile, std::ptr::null_mut());
        libr::set(libr::ptr_R_WriteConsole, None);
        libr::set(libr::ptr_R_WriteConsoleEx, Some(r_write_console));
        libr::set(libr::ptr_R_ReadConsole, Some(mcp_r_read_console));
        libr::set(libr::ptr_R_ShowMessage, Some(r_show_message));
        libr::set(libr::ptr_R_Busy, Some(r_busy));
        libr::setup_Rmainloop();
    }

    libraries.initialize_post_setup_r();
    unsafe {
        harp::CONSOLE_THREAD_ID = Some(thread::current().id());
    }
    harp::routines::r_register_routines();
    harp::initialize();
    harp::parse_eval_base("base::options(width = 200L)")?;
    initialize_r_repl()?;
    Ok(())
}

fn initialize_r_repl() -> Result<(), Box<dyn Error>> {
    let library = libloading::os::unix::Library::this();
    let init = unsafe { *library.get::<ReplInit>(b"R_ReplDLLinit\0")? };
    let do_one = unsafe { *library.get::<ReplDoOne>(b"R_ReplDLLdo1\0")? };
    let top_level_exec = unsafe { *library.get::<TopLevelExec>(b"R_ToplevelExec\0")? };
    let check_activity = unsafe { *library.get::<CheckActivity>(b"R_checkActivity\0")? };
    let run_handlers = unsafe { *library.get::<RunHandlers>(b"R_runHandlers\0")? };
    let check_interrupt = unsafe { *library.get::<CheckUserInterrupt>(b"R_CheckUserInterrupt\0")? };
    let add_input_handler = unsafe { *library.get::<AddInputHandler>(b"addInputHandler\0")? };
    let remove_input_handler =
        unsafe { *library.get::<RemoveInputHandler>(b"removeInputHandler\0")? };
    let rg_wait_usec = unsafe { *library.get::<*mut c_int>(b"Rg_wait_usec\0")? as usize };
    R_REPL_INIT
        .set(init)
        .map_err(|_| io::Error::other("R REPL was already initialized"))?;
    R_REPL_DO_ONE
        .set(do_one)
        .map_err(|_| io::Error::other("R REPL was already initialized"))?;
    R_EVENTS
        .set(REvents {
            top_level_exec,
            check_activity,
            run_handlers,
            add_input_handler,
            remove_input_handler,
            rg_wait_usec,
        })
        .map_err(|_| io::Error::other("R event handlers were already initialized"))?;
    R_CHECK_USER_INTERRUPT
        .set(check_interrupt)
        .map_err(|_| io::Error::other("R interrupt checker was already initialized"))?;
    unsafe { mcp_r_console_configure(r_read_console, check_interrupt, libr::R_interrupts_pending) };
    Ok(())
}

fn run_ready_handlers(graphics: &crate::r_graphics::Bridge) -> Result<(), String> {
    defer_interrupts(|| graphics.begin(), check_interrupts)?;
    EVALUATION_STARTED.store(true, Ordering::SeqCst);
    let events = R_EVENTS
        .get()
        .expect("R event handlers should be initialized");
    unsafe {
        mcp_r_run_ready_handlers(
            events.top_level_exec,
            events.check_activity,
            events.run_handlers,
            r_input_handlers(),
        );
    }
    EVALUATION_STARTED.store(false, Ordering::SeqCst);
    finish_console_stdin_operation()?;
    defer_interrupts(|| graphics.finish(), check_interrupts)?;
    observe_stdin_shutdown()
}

fn wait_for_activity(sideband_fd: c_int) -> Result<bool, String> {
    let events = R_EVENTS
        .get()
        .expect("R event handlers should be initialized");
    let mut wait_usec = unsafe { libr::get(libr::R_wait_usec) };
    let graphical_wait_usec = unsafe { *(events.rg_wait_usec as *const c_int) };
    if graphical_wait_usec > 0 && (wait_usec <= 0 || graphical_wait_usec < wait_usec) {
        wait_usec = graphical_wait_usec;
    }
    let status = unsafe {
        mcp_r_wait_for_activity(
            events.top_level_exec,
            events.add_input_handler,
            events.remove_input_handler,
            events.check_activity,
            libr::R_InputHandlers.cast(),
            sideband_fd,
            wait_usec,
        )
    };
    match status {
        0 => Ok(false),
        1 => Ok(true),
        _ => Err("R event wait failed".to_string()),
    }
}

fn r_input_handlers() -> *mut c_void {
    unsafe { libr::get(libr::R_InputHandlers).cast_mut() }
}

fn run_repl_cell() -> c_int {
    let init = *R_REPL_INIT
        .get()
        .expect("R REPL should be initialized before evaluation");
    let do_one = *R_REPL_DO_ONE
        .get()
        .expect("R REPL should be initialized before evaluation");
    let check_interrupt = *R_CHECK_USER_INTERRUPT
        .get()
        .expect("R interrupt checker should be initialized before evaluation");
    // SAFETY: Both function pointers are process-lifetime libR symbols with
    // the declared ABI. This main thread owns R, and the C shim contains R's
    // top-level jump so it cannot bypass a live Rust frame.
    unsafe {
        mcp_r_repl_run_cell(
            init,
            do_one,
            before_repl_iteration,
            check_interrupt,
            libr::R_interrupts_pending,
        )
    }
}

extern "C" fn before_repl_iteration() {
    // R may reuse buffered source without calling Busy(0), so reset before
    // every outer DLL step. Busy(1) latches evaluation in r_busy().
    EVALUATION_STARTED.store(false, Ordering::SeqCst);
}

extern "C-unwind" fn r_busy(which: c_int) {
    // ReadConsole serves cell source before Busy(1) and evaluated-code input
    // afterwards. Ignore Busy(0): a nested R REPL can issue it before a
    // ReadConsole request that still belongs to the evaluation.
    if which != 0 {
        EVALUATION_STARTED.store(true, Ordering::SeqCst);
    }
}

fn set_cell_source(mut source: String) {
    if !source.ends_with('\n') {
        source.push('\n');
    }
    *CELL_SOURCE
        .lock()
        .expect("R cell source lock should not be poisoned") = Some(CellSource {
        text: source,
        offset: 0,
    });
}

fn take_cell_source(max: usize) -> Option<Vec<u8>> {
    let mut source = CELL_SOURCE
        .lock()
        .expect("R cell source lock should not be poisoned");
    let source = source
        .as_mut()
        .expect("R cell source should be installed during evaluation");

    if source.offset == source.text.len() {
        return None;
    }
    let bytes = source.text.as_bytes();
    let line_length = bytes[source.offset..]
        .iter()
        .position(|byte| *byte == b'\n')
        .map_or(bytes.len() - source.offset, |index| index + 1);
    let mut length = line_length.min(max);
    while length > 0 && !source.text.is_char_boundary(source.offset + length) {
        length -= 1;
    }
    assert!(length > 0, "R console buffer is too small for UTF-8 source");
    let start = source.offset;
    let end = start + length;
    let chunk = bytes[start..end].to_vec();
    source.offset = end;
    Some(chunk)
}

fn clear_cell_source() {
    *CELL_SOURCE
        .lock()
        .expect("R cell source lock should not be poisoned") = None;
}

fn write_console_input(buf: *mut c_uchar, buflen: c_int, input: &[u8]) -> c_int {
    assert!(input.len() < buflen as usize);
    unsafe {
        std::ptr::copy_nonoverlapping(input.as_ptr(), buf, input.len());
        *buf.add(input.len()) = 0;
    }
    1
}

fn console_eof(buf: *mut c_uchar) -> c_int {
    unsafe {
        *buf = 0;
    }
    0
}

fn record_worker_failure(message: String) {
    let mut failure = WORKER_FAILURE
        .lock()
        .expect("R worker failure lock should not be poisoned");
    if failure.is_none() {
        *failure = Some(message);
    }
}

fn take_worker_failure() -> Option<String> {
    WORKER_FAILURE
        .lock()
        .expect("R worker failure lock should not be poisoned")
        .take()
}

fn emit_output(channel: ConsoleChannel, bytes: &[u8]) {
    if let Err(error) = send_output(channel, bytes) {
        record_worker_failure(error);
    }
}

fn send_output(channel: ConsoleChannel, bytes: &[u8]) -> Result<(), String> {
    if !crate::sideband::available_in_process() {
        return Ok(());
    }
    let Some(writer) = WORKER_WRITER.get() else {
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

extern "C-unwind" fn r_write_console(buf: *const c_char, buflen: c_int, otype: c_int) {
    if buf.is_null() || buflen <= 0 {
        return;
    }
    let bytes = unsafe { std::slice::from_raw_parts(buf.cast::<u8>(), buflen as usize) };
    let channel = if otype == 0 {
        ConsoleChannel::Output
    } else {
        ConsoleChannel::Diagnostic
    };
    emit_output(channel, bytes);
}

extern "C-unwind" fn r_show_message(buf: *const c_char) {
    if buf.is_null() {
        return;
    }
    let mut message = unsafe { CStr::from_ptr(buf) }.to_bytes().to_vec();
    message.push(b'\n');
    emit_output(ConsoleChannel::Diagnostic, &message);
}

extern "C-unwind" fn r_read_console(
    prompt: *const c_char,
    buf: *mut c_uchar,
    buflen: c_int,
    _add_history: c_int,
) -> c_int {
    if buf.is_null() || buflen <= 1 {
        return 0;
    }
    if !crate::sideband::available_in_process() {
        return console_eof(buf);
    }
    if !EVALUATION_STARTED.load(Ordering::SeqCst) {
        return match take_cell_source((buflen as usize) - 1) {
            Some(source) => write_console_input(buf, buflen, &source),
            None => console_eof(buf),
        };
    }

    let prompt = if prompt.is_null() {
        String::new()
    } else {
        unsafe { CStr::from_ptr(prompt) }
            .to_string_lossy()
            .into_owned()
    };
    if let Err(error) = send_input_requested(&prompt) {
        record_worker_failure(error);
        return console_eof(buf);
    }

    match read_console_stdin(buf, buflen) {
        Ok(read) => {
            let receipt = if read < 0 {
                send_input_cancelled()
            } else if read != 0 {
                send_input_received()
            } else {
                Ok(())
            };
            if let Err(error) = receipt {
                record_worker_failure(error);
                return console_eof(buf);
            }
            read
        }
        Err(error) => {
            record_worker_failure(error);
            console_eof(buf)
        }
    }
}

fn send_input_requested(prompt: &str) -> Result<(), String> {
    WORKER_WRITER
        .get()
        .expect("R worker sideband writer should be initialized")
        .send(&WorkerMessage::InputRequested {
            prompt: prompt.to_string(),
        })
        .map_err(|error| format!("R worker failed to report an input request: {error}"))
}

fn send_input_received() -> Result<(), String> {
    WORKER_WRITER
        .get()
        .expect("R worker sideband writer should be initialized")
        .send(&WorkerMessage::InputReceived)
        .map_err(|error| format!("R worker failed to report received input: {error}"))
}

fn send_input_cancelled() -> Result<(), String> {
    WORKER_WRITER
        .get()
        .expect("R worker sideband writer should be initialized")
        .send(&WorkerMessage::InputCancelled)
        .map_err(|error| format!("R worker failed to cancel an input request: {error}"))
}

fn send_image(data: String) -> Result<(), String> {
    WORKER_WRITER
        .get()
        .expect("R worker sideband writer should be initialized")
        .send(&WorkerMessage::Image {
            data,
            mime_type: "image/png".to_string(),
        })
        .map_err(|error| format!("R worker failed to send a plot image: {error}"))
}

pub(crate) fn publish_plot(image: Result<String, String>) {
    if let Err(error) = image.and_then(send_image) {
        record_worker_failure(error);
    }
}

fn read_console_stdin(buf: *mut c_uchar, buflen: c_int) -> Result<c_int, String> {
    let capacity = (buflen as usize) - 1;
    if console_interrupt_pending() {
        return cancel_console_stdin_read(buf, 0);
    }
    let mut stdin = CONSOLE_STDIN
        .lock()
        .map_err(|_| "R worker console stdin lock poisoned".to_string())?;
    // SAFETY: r_read_console validated buf and reserved one byte for NUL.
    let mut length = unsafe { stdin.copy_pushback(buf, capacity) };
    drop(stdin);

    while length < capacity {
        if console_interrupt_pending() {
            return cancel_console_stdin_read(buf, length);
        }
        let mut descriptor = libc::pollfd {
            fd: libc::STDIN_FILENO,
            events: libc::POLLIN,
            revents: 0,
        };
        let ready = unsafe { libc::poll(&mut descriptor, 1, 10) };
        if ready == 0 {
            continue;
        }
        if ready < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(format!("R worker stdin poll failed: {error}"));
        }
        if descriptor.revents & libc::POLLNVAL != 0 {
            return Err("R worker stdin descriptor is invalid".to_string());
        }
        if descriptor.revents & (libc::POLLIN | libc::POLLHUP) == 0 {
            return Err(format!(
                "R worker stdin poll returned unexpected events {}",
                descriptor.revents
            ));
        }
        if console_interrupt_pending() {
            return cancel_console_stdin_read(buf, length);
        }
        let byte = unsafe { buf.add(length) };
        let count = unsafe { libc::read(libc::STDIN_FILENO, byte.cast(), 1) };
        if count == 1 {
            length += 1;
            if unsafe { *byte } == b'\n' {
                break;
            }
            continue;
        }
        if count == 0 {
            WORKER_SHUTDOWN.store(true, Ordering::SeqCst);
            return Ok(console_eof(buf));
        }

        let error = io::Error::last_os_error();
        if error.kind() == io::ErrorKind::Interrupted {
            continue;
        }
        return Err(format!("R worker stdin read failed: {error}"));
    }
    unsafe {
        *buf.add(length) = 0;
    }
    record_console_stdin_chunk(buf, length)?;
    Ok(i32::from(length > 0))
}

fn record_console_stdin_chunk(buf: *const c_uchar, length: usize) -> Result<(), String> {
    let chunk = unsafe { std::slice::from_raw_parts(buf, length) };
    let mut stdin = CONSOLE_STDIN
        .lock()
        .map_err(|_| "R worker console stdin lock poisoned".to_string())?;
    stdin.record_chunk(chunk);
    Ok(())
}

fn cancel_console_stdin_read(buf: *const c_uchar, length: usize) -> Result<c_int, String> {
    let chunk = unsafe { std::slice::from_raw_parts(buf, length) };
    let mut stdin = CONSOLE_STDIN
        .lock()
        .map_err(|_| "R worker console stdin lock poisoned".to_string())?;
    stdin.preserve_line(chunk);
    Ok(-1)
}

fn finish_console_stdin_operation() -> Result<(), String> {
    let mut stdin = CONSOLE_STDIN
        .lock()
        .map_err(|_| "R worker console stdin lock poisoned".to_string())?;
    stdin.finish_operation();
    Ok(())
}
