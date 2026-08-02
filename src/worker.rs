#[cfg(target_os = "macos")]
mod platform {
    use std::error::Error;
    use std::ffi::{CStr, CString, c_char, c_int, c_uchar};
    use std::io;
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
    use std::sync::{Mutex, OnceLock};
    use std::thread;

    use crate::cell::{Cell, Language};
    use crate::worker_protocol::{ServerMessage, WorkerMessage};

    static R_MAIN_ARGS: OnceLock<Vec<CString>> = OnceLock::new();
    static WORKER_WRITER: OnceLock<crate::sideband::Writer> = OnceLock::new();
    static R_REPL_INIT: OnceLock<ReplInit> = OnceLock::new();
    static R_REPL_DO_ONE: OnceLock<ReplDoOne> = OnceLock::new();
    static CELL_SOURCE: Mutex<Option<CellSource>> = Mutex::new(None);
    static WORKER_FAILURE: Mutex<Option<String>> = Mutex::new(None);
    static WORKER_SHUTDOWN: AtomicBool = AtomicBool::new(false);
    static EVALUATION_STARTED: AtomicBool = AtomicBool::new(false);
    static NEXT_PYTHON_EVALUATION_ID: AtomicU64 = AtomicU64::new(1);
    static R_TRY_EVAL: OnceLock<TryEval> = OnceLock::new();
    static mut PYTHON_STATE: libr::SEXP = libr::SEXP::null();

    const PYTHON_BRIDGE_INIT: &str = r#"
base::local({
  evaluator <- NULL
  source <- NULL

  evaluate <- function(id) {
    if (is.null(evaluator)) {
      if (!reticulate::py_available(initialize = FALSE) &&
          !nzchar(Sys.getenv("RETICULATE_PYTHON"))) {
        python <- Sys.which("python3")
        if (!nzchar(python)) {
          stop("python3 was not found on PATH", call. = FALSE)
        }
        reticulate::use_python(python, required = TRUE)
      }

      private <- reticulate::py_run_string(r"---(
import __main__ as _main
import ast as _ast
import builtins as _builtins
import sys as _sys
import traceback as _traceback


def _mcp_console_eval_cell(
    source,
    filename,
    _main=_main,
    _parse=_ast.parse,
    _Expr=_ast.Expr,
    _Expression=_ast.Expression,
    _isinstance=_builtins.isinstance,
    _compile=_builtins.compile,
    _exec=_builtins.exec,
    _eval=_builtins.eval,
    _BaseException=_builtins.BaseException,
    _sys=_sys,
    _print_exc=_traceback.print_exc,
):
    try:
        module = _parse(source, filename=filename, mode="exec")
        final = module.body[-1] if module.body else None
        if _isinstance(final, _Expr):
            module.body.pop()
            statements = _compile(module, filename, "exec") if module.body else None
            expression = _compile(_Expression(final.value), filename, "eval")
        else:
            statements = _compile(module, filename, "exec")
            expression = None

        if statements is not None:
            _exec(statements, _main.__dict__)
        if expression is not None:
            _sys.displayhook(_eval(expression, _main.__dict__))
    except _BaseException:
        _print_exc()
)---", local = TRUE, convert = FALSE)
      evaluator <<- private$`_mcp_console_eval_cell`
    }

    filename <- paste0("<mcp-console:python:", id, ">")
    invisible(evaluator(source, filename))
  }

  environment()
}, envir = base::new.env(parent = base::baseenv()))
"#;

    type ReplInit = unsafe extern "C-unwind" fn();
    type ReplDoOne = unsafe extern "C-unwind" fn() -> c_int;
    type TryEval = unsafe extern "C-unwind" fn(libr::SEXP, libr::SEXP, *mut c_int) -> libr::SEXP;

    struct CellSource {
        text: String,
        offset: usize,
    }

    unsafe extern "C" {
        fn mcp_r_repl_run_cell(
            init: ReplInit,
            do_one: ReplDoOne,
            before_do_one: extern "C" fn(),
        ) -> c_int;
    }

    pub(crate) fn run() -> Result<(), Box<dyn Error>> {
        // SAFETY: pthread_main_np has no preconditions.
        if unsafe { libc::pthread_main_np() } != 1 {
            return Err(io::Error::other("R worker must run on the process main thread").into());
        }
        // The worker owns its environment and is still single-threaded. Reticulate
        // must replace Python's fd-backed streams before user R can initialize it.
        if unsafe {
            libc::setenv(
                c"RETICULATE_REMAP_OUTPUT_STREAMS".as_ptr(),
                c"1".as_ptr(),
                1,
            )
        } != 0
        {
            return Err(io::Error::last_os_error().into());
        }
        let (mut reader, writer) = crate::sideband::connect_from_env()?;
        let r_home = harp::command::r_home_setup()?;
        initialize_r(&r_home)?;
        WORKER_WRITER
            .set(writer.clone())
            .map_err(|_| io::Error::other("R worker sideband was already initialized"))?;
        initialize_python_bridge()?;
        writer.send(&WorkerMessage::Ready)?;

        loop {
            match reader.receive()? {
                ServerMessage::Evaluate { language, source } => {
                    let result = evaluate_cell(Cell { language, source });

                    if WORKER_SHUTDOWN.load(Ordering::SeqCst) {
                        return Ok(());
                    }
                    if let Some(message) = take_worker_failure().or_else(|| result.err()) {
                        return Err(io::Error::other(message).into());
                    }
                    writer.send(&WorkerMessage::Completed)?;
                }
                ServerMessage::Shutdown => return Ok(()),
            }
        }
    }

    fn evaluate_cell(cell: Cell) -> Result<(), String> {
        match cell.language {
            Language::R => evaluate_r_cell(cell.source),
            Language::Python => evaluate_python_cell(cell.source),
        }
    }

    fn evaluate_r_cell(r: String) -> Result<(), String> {
        if r.contains('\0') {
            emit_output(b"Error: R source cannot contain NUL\n");
            return Ok(());
        }

        set_cell_source(r);
        let status = run_repl_cell();
        clear_cell_source();
        match status {
            0 | 1 => Ok(()),
            2 => {
                emit_output(b"Error: Incomplete code\n");
                Ok(())
            }
            status => Err(format!(
                "R worker received unexpected DLL REPL status {status}"
            )),
        }
    }

    fn evaluate_python_cell(python: String) -> Result<(), String> {
        if python.contains('\0') {
            emit_output(b"SyntaxError: source code string cannot contain null bytes\n");
            return Ok(());
        }
        let evaluation_id = NEXT_PYTHON_EVALUATION_ID.fetch_add(1, Ordering::Relaxed);
        let evaluation_id = format!("e{evaluation_id}");
        call_python_bridge(&python, &evaluation_id)
    }

    fn initialize_python_bridge() -> Result<(), String> {
        let source_length = c_int::try_from(PYTHON_BRIDGE_INIT.len())
            .expect("the fixed Python bridge should fit in an R string");
        let (parse_status, expression_count, evaluation_error, state, is_environment) =
            harp::top_level_exec(|| {
                // SAFETY: This runs on R's main thread. The top-level boundary
                // contains parser allocation failures, and R_tryEval contains
                // errors raised while evaluating the fixed bridge expression.
                unsafe {
                    let source = libr::Rf_protect(r_string(PYTHON_BRIDGE_INIT, source_length));
                    let mut parse_status = libr::ParseStatus_PARSE_NULL;
                    let expressions = libr::Rf_protect(libr::R_ParseVector(
                        source,
                        -1,
                        &mut parse_status,
                        libr::R_NilValue,
                    ));
                    let expression_count = libr::Rf_xlength(expressions);
                    if parse_status != libr::ParseStatus_PARSE_OK || expression_count != 1 {
                        libr::Rf_unprotect(2);
                        return (parse_status, expression_count, 0, libr::SEXP::null(), false);
                    }

                    let expression = libr::VECTOR_ELT(expressions, 0);
                    let mut evaluation_error = 0;
                    let try_eval = R_TRY_EVAL
                        .get()
                        .expect("R_tryEval should be initialized before bridge setup");
                    let state = try_eval(expression, libr::R_BaseEnv, &mut evaluation_error);
                    if evaluation_error != 0 || state.is_null() {
                        libr::Rf_unprotect(2);
                        return (
                            parse_status,
                            expression_count,
                            evaluation_error,
                            state,
                            false,
                        );
                    }
                    let state = libr::Rf_protect(state);
                    let is_environment = libr::TYPEOF(state) == libr::ENVSXP as c_int;
                    if is_environment {
                        libr::R_PreserveObject(state);
                    }
                    libr::Rf_unprotect(3);
                    (
                        parse_status,
                        expression_count,
                        evaluation_error,
                        state,
                        is_environment,
                    )
                }
            })
            .map_err(|error| format!("failed to initialize the Python bridge: {error}"))?;
        if parse_status != libr::ParseStatus_PARSE_OK || expression_count != 1 {
            return Err(format!(
                "Python bridge initialization parsed with status {parse_status} and produced {expression_count} expressions"
            ));
        }
        if evaluation_error != 0 {
            return Err("Python bridge initialization failed during R evaluation".to_string());
        }
        if state.is_null() {
            return Err("Python state initialization returned a null R object".to_string());
        }
        if !is_environment {
            return Err("Python state initialization did not produce an environment".to_string());
        }
        // SAFETY: The worker runs R on this process main thread. R preserves
        // this object until process exit, and every later access is on that thread.
        unsafe {
            PYTHON_STATE = state;
        }
        Ok(())
    }

    fn call_python_bridge(source: &str, evaluation_id: &str) -> Result<(), String> {
        let source_length = c_int::try_from(source.len())
            .map_err(|_| "Python source exceeds R's maximum string size".to_string())?;
        let evaluation_id_length = c_int::try_from(evaluation_id.len())
            .expect("generated evaluation IDs should fit in an R string");
        EVALUATION_STARTED.store(true, Ordering::SeqCst);
        let result = harp::top_level_exec(|| {
            // SAFETY: This runs on R's main thread. The outer top-level
            // boundary contains allocation errors; R_tryEval contains errors
            // raised while the preserved private environment calls reticulate.
            unsafe {
                let source = libr::Rf_protect(r_string(source, source_length));
                let evaluation_id = libr::Rf_protect(r_string(evaluation_id, evaluation_id_length));
                let source_symbol = libr::Rf_install(c"source".as_ptr());
                let evaluate_symbol = libr::Rf_install(c"evaluate".as_ptr());
                libr::Rf_defineVar(source_symbol, source, PYTHON_STATE);
                let call = libr::Rf_protect(libr::Rf_lang2(evaluate_symbol, evaluation_id));
                let try_eval = R_TRY_EVAL
                    .get()
                    .expect("R_tryEval should be initialized before Python evaluation");
                try_eval(call, PYTHON_STATE, std::ptr::null_mut());
                libr::Rf_defineVar(source_symbol, libr::R_NilValue, PYTHON_STATE);
                libr::Rf_unprotect(3);
            }
        });
        EVALUATION_STARTED.store(false, Ordering::SeqCst);
        result.map_err(|error| format!("failed to call the Python bridge: {error}"))
    }

    fn r_string(value: &str, length: c_int) -> libr::SEXP {
        // SAFETY: The caller runs under R's top-level allocation boundary and
        // immediately protects the returned scalar string.
        unsafe {
            let value = libr::Rf_mkCharLenCE(value.as_ptr().cast(), length, libr::cetype_t_CE_UTF8);
            libr::Rf_ScalarString(value)
        }
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
            libr::set(libr::ptr_R_ReadConsole, Some(r_read_console));
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
        initialize_r_repl()?;
        Ok(())
    }

    fn initialize_r_repl() -> Result<(), Box<dyn Error>> {
        let library = libloading::os::unix::Library::this();
        let init = unsafe { *library.get::<ReplInit>(b"R_ReplDLLinit\0")? };
        let do_one = unsafe { *library.get::<ReplDoOne>(b"R_ReplDLLdo1\0")? };
        let try_eval = unsafe { *library.get::<TryEval>(b"R_tryEval\0")? };
        R_REPL_INIT
            .set(init)
            .map_err(|_| io::Error::other("R REPL was already initialized"))?;
        R_REPL_DO_ONE
            .set(do_one)
            .map_err(|_| io::Error::other("R REPL was already initialized"))?;
        R_TRY_EVAL
            .set(try_eval)
            .map_err(|_| io::Error::other("R_tryEval was already initialized"))?;
        Ok(())
    }

    fn run_repl_cell() -> c_int {
        let init = *R_REPL_INIT
            .get()
            .expect("R REPL should be initialized before evaluation");
        let do_one = *R_REPL_DO_ONE
            .get()
            .expect("R REPL should be initialized before evaluation");
        // SAFETY: Both function pointers are process-lifetime libR symbols with
        // the declared ABI. This main thread owns R, and the C shim contains R's
        // top-level jump so it cannot bypass a live Rust frame.
        unsafe { mcp_r_repl_run_cell(init, do_one, before_repl_iteration) }
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

    fn emit_output(bytes: &[u8]) {
        if !crate::sideband::available_in_process() {
            return;
        }
        let Some(writer) = WORKER_WRITER.get() else {
            return;
        };
        if WORKER_FAILURE.lock().is_ok_and(|failure| failure.is_some()) {
            return;
        }
        if let Err(error) = writer.send(&WorkerMessage::Output {
            data: String::from_utf8_lossy(bytes).into_owned(),
        }) {
            record_worker_failure(format!("R console output failed: {error}"));
        }
    }

    extern "C-unwind" fn r_write_console(buf: *const c_char, buflen: c_int, _otype: c_int) {
        if buf.is_null() || buflen <= 0 {
            return;
        }
        let bytes = unsafe { std::slice::from_raw_parts(buf.cast::<u8>(), buflen as usize) };
        emit_output(bytes);
    }

    extern "C-unwind" fn r_show_message(buf: *const c_char) {
        if buf.is_null() {
            return;
        }
        let mut message = unsafe { CStr::from_ptr(buf) }.to_bytes().to_vec();
        message.push(b'\n');
        emit_output(&message);
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
                if read != 0
                    && let Err(error) = send_input_received()
                {
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

    fn read_console_stdin(buf: *mut c_uchar, buflen: c_int) -> Result<c_int, String> {
        let mut length = 0;
        while length < (buflen as usize) - 1 {
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
            if error.kind() != io::ErrorKind::Interrupted {
                return Err(format!("R worker stdin read failed: {error}"));
            }
        }
        unsafe {
            *buf.add(length) = 0;
        }
        Ok(i32::from(length > 0))
    }
}

#[cfg(target_os = "macos")]
pub(crate) use platform::run;

#[cfg(not(target_os = "macos"))]
pub(crate) fn run() -> Result<(), Box<dyn std::error::Error>> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "embedded R workers are supported only on macOS",
    )
    .into())
}
