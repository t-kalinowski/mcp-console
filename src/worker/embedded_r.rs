use std::error::Error;
use std::ffi::{CStr, CString, c_char, c_int, c_uchar, c_void};
use std::io;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use std::thread;

use super::core::{
    self, emit_output, observe_stdin_shutdown, record_worker_failure, send_input_cancelled,
    send_input_received, send_input_requested,
};
use super::input::{finish_console_stdin_operation, read_console_stdin};
use crate::cell::Language;
use crate::worker_protocol::ConsoleChannel;

static R_MAIN_ARGS: OnceLock<Vec<CString>> = OnceLock::new();
static R_EVENTS: OnceLock<REvents> = OnceLock::new();
static R_CHECK_USER_INTERRUPT: OnceLock<CheckUserInterrupt> = OnceLock::new();
static CELL_SOURCE: Mutex<Option<CellSource>> = Mutex::new(None);
static EVALUATION_STARTED: AtomicBool = AtomicBool::new(false);
static SQL_EVALUATION_STARTED: AtomicBool = AtomicBool::new(false);
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
type ExecWithCleanup = unsafe extern "C-unwind" fn(
    unsafe extern "C-unwind" fn(*mut c_void) -> *mut c_void,
    *mut c_void,
    unsafe extern "C-unwind" fn(*mut c_void),
    *mut c_void,
) -> *mut c_void;
type ObjectFn = unsafe extern "C-unwind" fn(*mut c_void);

#[repr(C)]
struct ReplApi {
    init: ReplInit,
    do_one: ReplDoOne,
    top_level_exec: TopLevelExec,
    exec_with_cleanup: ExecWithCleanup,
    preserve: ObjectFn,
    release: ObjectFn,
    stack_top: *mut c_int,
    stack: *mut *mut *mut c_void,
    nil: libr::SEXP,
}
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

struct REvents {
    top_level_exec: TopLevelExec,
    check_activity: CheckActivity,
    run_handlers: RunHandlers,
    add_input_handler: AddInputHandler,
    remove_input_handler: RemoveInputHandler,
    rg_wait_usec: usize,
}

pub(super) struct Runtime {
    graphics: crate::r_graphics::Bridge,
    environment: crate::r_environment::Bridge,
}

impl Runtime {
    pub(super) fn initialize() -> Result<Self, Box<dyn Error>> {
        Ok(Self {
            graphics: crate::r_graphics::Bridge::initialize()?,
            environment: crate::r_environment::Bridge::initialize()?,
        })
    }

    pub(super) fn temporary_directory() -> Result<std::path::PathBuf, Box<dyn Error>> {
        Ok(String::try_from(harp::parse_eval_base("base::tempdir()")?)?.into())
    }

    pub(super) fn idle(&self) -> Result<(), String> {
        run_ready_handlers(&self.graphics)
    }

    pub(super) fn prepare(
        &self,
        library: &str,
    ) -> Result<crate::r_environment::PreparationOutcome, String> {
        defer_interrupts(
            || self.environment.prepare(std::path::Path::new(library)),
            discard_interrupts,
        )
    }

    pub(super) fn begin_cell(&self, language: Language) -> Result<(), String> {
        if !matches!(language, Language::Sql) {
            defer_interrupts(|| self.graphics.begin(), check_interrupts)?;
        }
        if !matches!(language, Language::R) {
            EVALUATION_STARTED.store(true, Ordering::SeqCst);
        }
        SQL_EVALUATION_STARTED.store(matches!(language, Language::Sql), Ordering::SeqCst);
        Ok(())
    }

    pub(super) fn finish_cell(&self, language: Language) -> Result<(), String> {
        if !matches!(language, Language::R) {
            EVALUATION_STARTED.store(false, Ordering::SeqCst);
        }
        SQL_EVALUATION_STARTED.store(false, Ordering::SeqCst);
        if !matches!(language, Language::Sql) {
            defer_interrupts(|| self.graphics.finish(), check_interrupts)?;
        }
        Ok(())
    }

    pub(super) fn evaluate(&self, source: String) -> Result<(), String> {
        evaluate_r_cell(source)
    }
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
    fn mcp_r_repl_configure(api: *const ReplApi);
    fn mcp_r_repl_run_cell(before_do_one: extern "C" fn()) -> c_int;
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

pub(super) fn normalize_interrupt_signal() -> io::Result<()> {
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

pub(super) fn check_interrupts() {
    if !interrupt_pending() {
        return;
    }
    let check = *R_CHECK_USER_INTERRUPT
        .get()
        .expect("R interrupt checker should be initialized");
    let _ = harp::top_level_exec(|| unsafe { check() });
}

pub(super) fn defer_interrupts<T>(
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

pub(super) fn discard_interrupts() {
    unsafe { libr::set(libr::R_interrupts_pending, 0) };
}

fn interrupt_pending() -> bool {
    unsafe { libr::get(libr::R_interrupts_pending) != 0 }
}

fn console_interrupt_pending() -> bool {
    interrupt_pending()
        && unsafe { libr::get(libr::R_interrupts_suspended) == libr::Rboolean_FALSE }
}

pub(crate) fn resolve_r(
    packages: Vec<String>,
) -> Result<crate::r_environment::ResolutionOutcome, String> {
    // SQL callbacks can reenter R, but SQL evaluation does not resolve packages.
    if SQL_EVALUATION_STARTED.load(Ordering::SeqCst) {
        return Ok(crate::r_environment::ResolutionOutcome::Unavailable);
    }
    core::resolve_r(packages)
}

fn evaluate_r_cell(r: String) -> Result<(), String> {
    set_cell_source(r);
    let status = run_repl_cell();
    clear_cell_source();
    match status {
        0 | 1 => Ok(()),
        2 => {
            emit_output(ConsoleChannel::Diagnostic, b"Error: Incomplete code\n");
            Ok(())
        }
        status => Err(format!(
            "R worker received unexpected DLL REPL status {status}"
        )),
    }
}

pub(super) fn initialize_r(r_home: &std::path::Path) -> Result<(), Box<dyn Error>> {
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
    unsafe {
        mcp_r_repl_configure(&ReplApi {
            init,
            do_one,
            top_level_exec,
            exec_with_cleanup: *library.get::<ExecWithCleanup>(b"R_ExecWithCleanup\0")?,
            preserve: *library.get::<ObjectFn>(b"R_PreserveObject\0")?,
            release: *library.get::<ObjectFn>(b"R_ReleaseObject\0")?,
            stack_top: *library.get::<*mut c_int>(b"R_PPStackTop\0")?,
            stack: *library.get::<*mut *mut *mut c_void>(b"R_PPStack\0")?,
            nil: libr::R_NilValue,
        });
    }
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

pub(super) fn wait_for_activity(sideband_fd: c_int) -> Result<bool, String> {
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
    // SAFETY: The configured API consists of process-lifetime libR symbols.
    // This main thread owns R; the C shim keeps a live top-level context so
    // errors and interrupts cannot bypass a Rust frame.
    unsafe { mcp_r_repl_run_cell(before_repl_iteration) }
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

    match read_console_stdin(buf, buflen, console_interrupt_pending) {
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
