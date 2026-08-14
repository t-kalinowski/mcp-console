#[cfg(target_os = "macos")]
mod platform {
    use std::error::Error;
    use std::ffi::{CStr, CString, c_char, c_int, c_uchar, c_void};
    use std::io;
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
    static CELL_SOURCE: Mutex<Option<CellSource>> = Mutex::new(None);
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

    struct CellSource {
        text: String,
        offset: usize,
    }

    struct REvents {
        top_level_exec: TopLevelExec,
        check_activity: CheckActivity,
        run_handlers: RunHandlers,
    }

    struct Runtime {
        writer: crate::sideband::Writer,
        graphics: crate::r_graphics::Bridge,
        r_environment: crate::r_environment::Bridge,
        python: crate::python::Bridge,
        sql: crate::sql::Bridge,
    }

    unsafe extern "C" {
        fn mcp_r_run_ready_handlers(
            top_level_exec: TopLevelExec,
            check_activity: CheckActivity,
            run_handlers: RunHandlers,
            input_handlers: *mut c_void,
        );
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
        crate::python::configure_worker_environment()?;
        let (reader, writer) = crate::sideband::connect_from_env()?;
        let r_home = harp::command::r_home_setup()?;
        initialize_r(&r_home)?;
        WORKER_READER
            .set(Mutex::new(reader))
            .map_err(|_| io::Error::other("R worker sideband was already initialized"))?;
        WORKER_WRITER
            .set(writer.clone())
            .map_err(|_| io::Error::other("R worker sideband was already initialized"))?;
        let graphics = crate::r_graphics::Bridge::initialize()?;
        let r_environment = crate::r_environment::Bridge::initialize()?;
        let python = crate::python::Bridge::initialize()?;
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
                if !self.handle(receive_server_message()?)? {
                    return Ok(());
                }
            }
        }

        fn handle(&mut self, message: ServerMessage) -> Result<bool, Box<dyn Error>> {
            match message {
                ServerMessage::Evaluate { language, source } => {
                    let result = evaluate_cell(
                        Cell { language, source },
                        &self.graphics,
                        &mut self.python,
                        &mut self.sql,
                    );

                    if WORKER_SHUTDOWN.load(Ordering::SeqCst) {
                        return Ok(false);
                    }
                    if let Some(message) = take_worker_failure().or_else(|| result.err()) {
                        return Err(io::Error::other(message).into());
                    }
                    self.writer.send(&WorkerMessage::Completed {
                        python_checkpoint: self.python.checkpoint()?,
                    })?;
                }
                ServerMessage::PreparePython { packages } => {
                    let result = self.python.prepare(packages);
                    if WORKER_SHUTDOWN.load(Ordering::SeqCst) {
                        return Ok(false);
                    }
                    if let Some(message) = take_worker_failure() {
                        return Err(io::Error::other(message).into());
                    }
                    match result {
                        Ok(crate::python::PreparationOutcome::Prepared {
                            checkpoint: python_checkpoint,
                        }) => {
                            self.writer
                                .send(&WorkerMessage::PythonPrepared { python_checkpoint })?;
                        }
                        Ok(crate::python::PreparationOutcome::Failed { message }) => {
                            self.writer
                                .send(&WorkerMessage::PythonPreparationFailed { message })?;
                        }
                        Err(message) => return Err(io::Error::other(message).into()),
                    }
                }
                ServerMessage::PrepareR { library } => {
                    let result = self.r_environment.prepare(std::path::Path::new(&library));
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
                ServerMessage::PythonResolved { .. }
                | ServerMessage::PythonResolutionFailed { .. }
                | ServerMessage::PythonVersionResolved { .. }
                | ServerMessage::PythonVersionResolutionFailed { .. } => {
                    return Err(io::Error::other(
                        "worker received an unexpected Python resolver response",
                    )
                    .into());
                }
            }
            Ok(true)
        }
    }

    pub(crate) fn resolve_python(
        request: crate::worker_protocol::PythonResolveRequest,
    ) -> Result<String, String> {
        send_worker_message(&WorkerMessage::ResolvePython { request })?;
        match receive_server_message().map_err(infrastructure_failure)? {
            ServerMessage::PythonResolved { python } => {
                crate::python::link_matplotlib_caches();
                Ok(python)
            }
            ServerMessage::PythonResolutionFailed { message } => Err(message),
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

    pub(crate) fn resolve_python_version(
        request: crate::worker_protocol::PythonVersionResolveRequest,
    ) -> Result<String, String> {
        send_worker_message(&WorkerMessage::ResolvePythonVersion { request })?;
        match receive_server_message().map_err(infrastructure_failure)? {
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
            ServerMessage::PythonResolved { .. } | ServerMessage::PythonResolutionFailed { .. } => {
                Err(infrastructure_failure(
                    "worker received a Python environment response while resolving a Python version"
                        .to_string(),
                ))
            }
        }
    }

    fn receive_server_message() -> Result<ServerMessage, String> {
        WORKER_READER
            .get()
            .ok_or_else(|| "R worker sideband reader is not initialized".to_string())?
            .lock()
            .map_err(|_| "R worker sideband reader lock poisoned".to_string())?
            .receive()
            .map_err(|error| format!("worker sideband read failed: {error}"))
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
            return Err("managed Python resolution is unavailable in a fork child".to_string());
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
        python: &mut crate::python::Bridge,
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

        graphics.begin()?;
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
        graphics.finish()?;
        result
    }

    fn evaluate_python_cell(
        source: String,
        graphics: &crate::r_graphics::Bridge,
        python: &mut crate::python::Bridge,
    ) -> Result<(), String> {
        if source.contains('\0') {
            emit_output(
                ConsoleChannel::Diagnostic,
                b"SyntaxError: source code string cannot contain null bytes\n",
            );
            return Ok(());
        }
        graphics.begin()?;
        EVALUATION_STARTED.store(true, Ordering::SeqCst);
        let result = python.evaluate(&source);
        EVALUATION_STARTED.store(false, Ordering::SeqCst);
        graphics.finish()?;
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
            })
            .map_err(|_| io::Error::other("R event handlers were already initialized"))?;
        Ok(())
    }

    fn run_ready_handlers(graphics: &crate::r_graphics::Bridge) -> Result<(), String> {
        graphics.begin()?;
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
        graphics.finish()?;
        observe_stdin_shutdown()
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
pub(crate) use platform::{publish_plot, resolve_python, resolve_python_version, run};

#[cfg(not(target_os = "macos"))]
pub(crate) fn run() -> Result<(), Box<dyn std::error::Error>> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "embedded R workers are supported only on macOS",
    )
    .into())
}
