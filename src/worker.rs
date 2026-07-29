pub enum Boundary {
    Complete(String),
    Input(String),
}

#[cfg(target_family = "unix")]
mod unix {
    use std::error::Error;
    use std::ffi::{CStr, CString, c_char, c_int, c_uchar, c_void};
    use std::io::{self, Read};
    use std::os::unix::process::CommandExt;
    use std::path::Path;
    use std::process::{Child, Command, Stdio};
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
    use std::sync::{Arc, Mutex, OnceLock};
    use std::thread;
    use std::time::{Duration, Instant};

    use base64::Engine as _;
    use base64::engine::general_purpose::STANDARD;
    use serde::{Deserialize, Serialize};

    use crate::sideband;

    const OUTPUT_CHUNK_BYTES: usize = 8 * 1024;
    const SHUTDOWN_GRACE: Duration = Duration::from_secs(1);
    const CHILD_POLL_INTERVAL: Duration = Duration::from_millis(25);

    static R_MAIN_ARGS: OnceLock<Vec<CString>> = OnceLock::new();
    static WORKER_READER: OnceLock<Mutex<sideband::Reader>> = OnceLock::new();
    static WORKER_WRITER: OnceLock<sideband::Writer> = OnceLock::new();
    static R_REPL_INIT: OnceLock<unsafe extern "C-unwind" fn()> = OnceLock::new();
    static R_REPL_DO_ONE: OnceLock<unsafe extern "C-unwind" fn() -> c_int> = OnceLock::new();
    static mut TRACEBACK_CALL: Option<libr::SEXP> = None;
    static R_REPL_INPUT: Mutex<Option<Vec<u8>>> = Mutex::new(None);
    static INTERACTIVE_INPUT: Mutex<Vec<u8>> = Mutex::new(Vec::new());
    static WORKER_FAILURE: Mutex<Option<String>> = Mutex::new(None);
    static WORKER_SHUTDOWN: AtomicBool = AtomicBool::new(false);
    static INTERACTIVE_READ: AtomicBool = AtomicBool::new(false);
    static R_REPL_PROXY_COUNTER: AtomicU64 = AtomicU64::new(0);

    #[derive(Serialize, Deserialize)]
    #[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
    enum ServerMessage {
        Evaluate { r: String },
        Input { stdin: String },
        Shutdown,
    }

    #[derive(Serialize, Deserialize)]
    #[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
    enum WorkerMessage {
        Ready,
        Output { data_b64: String },
        InputRequested { prompt: String },
        InputPending { prompt: String },
        Completed,
        LanguageError { message: String },
        Fatal { message: String },
    }

    pub struct RWorker {
        reader: sideband::Reader,
        control: WorkerControl,
        _sandbox: crate::sandbox::WorkerCommand,
    }

    #[derive(Clone)]
    pub struct WorkerControl {
        writer: sideband::Writer,
        child: Arc<Mutex<Child>>,
    }

    impl RWorker {
        pub fn start(on_started: impl FnOnce(WorkerControl)) -> Result<Self, String> {
            let (mut reader, writer, child_fds) = sideband::bind()
                .map_err(|error| format!("failed to create R worker sideband: {error}"))?;
            let executable = std::env::current_exe()
                .map_err(|error| format!("failed to locate mcp-console: {error}"))?;
            let mut sandbox = crate::sandbox::worker_command(executable.as_os_str())?;
            restore_loader_environment(&mut sandbox);
            let command = sandbox.command_mut();
            command
                .arg("__worker_bootstrap")
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            child_fds.configure(command);

            let mut child = command
                .spawn()
                .map_err(|error| format!("failed to launch sandboxed R worker: {error}"))?;
            drop(child_fds);
            drain(child.stdout.take());
            drain(child.stderr.take());
            let child = Arc::new(Mutex::new(child));
            let control = WorkerControl { writer, child };
            on_started(control.clone());

            let message = match reader.receive::<WorkerMessage>() {
                Ok(message) => message,
                Err(error) => {
                    let message = worker_io_error(&control.child, error);
                    control.shutdown();
                    return Err(message);
                }
            };
            if !matches!(message, WorkerMessage::Ready) {
                control.shutdown();
                return Err("R worker did not report readiness".to_string());
            }

            Ok(Self {
                reader,
                control,
                _sandbox: sandbox,
            })
        }

        pub fn evaluate(&mut self, r: String) -> Result<super::Boundary, String> {
            self.control
                .writer
                .send(&ServerMessage::Evaluate { r })
                .map_err(|error| format!("R worker sideband write failed: {error}"))?;
            self.read_boundary()
        }

        pub fn provide_input(&mut self, stdin: String) -> Result<super::Boundary, String> {
            self.control
                .writer
                .send(&ServerMessage::Input { stdin })
                .map_err(|error| format!("R worker sideband write failed: {error}"))?;
            self.read_boundary()
        }

        pub fn control(&self) -> WorkerControl {
            self.control.clone()
        }

        fn read_boundary(&mut self) -> Result<super::Boundary, String> {
            let mut output = Vec::new();
            loop {
                let message = self.reader.receive::<WorkerMessage>().map_err(|error| {
                    format!(
                        "R worker sideband read failed: {}",
                        worker_io_error(&self.control.child, error)
                    )
                })?;
                match message {
                    WorkerMessage::Output { data_b64 } => {
                        let bytes = STANDARD
                            .decode(data_b64)
                            .map_err(|error| format!("R worker sent invalid output: {error}"))?;
                        output.extend_from_slice(&bytes);
                    }
                    WorkerMessage::InputRequested { prompt } => {
                        append_text(&mut output, prompt.trim_end());
                        append_marker(&mut output, "[input]");
                        return Ok(super::Boundary::Input(
                            String::from_utf8_lossy(&output).into_owned(),
                        ));
                    }
                    WorkerMessage::InputPending { prompt } => {
                        append_text(&mut output, prompt.trim_end());
                        append_marker(&mut output, "[input]");
                        return Ok(super::Boundary::Input(
                            String::from_utf8_lossy(&output).into_owned(),
                        ));
                    }
                    WorkerMessage::Completed => {
                        if output.is_empty() {
                            output.extend_from_slice(b"[done]");
                        }
                        return Ok(super::Boundary::Complete(
                            String::from_utf8_lossy(&output).into_owned(),
                        ));
                    }
                    WorkerMessage::LanguageError { message } => {
                        append_language_error(&mut output, &message);
                        return Ok(super::Boundary::Complete(
                            String::from_utf8_lossy(&output).into_owned(),
                        ));
                    }
                    WorkerMessage::Fatal { message } => return Err(message),
                    WorkerMessage::Ready => {
                        return Err("R worker sent an unexpected ready message".to_string());
                    }
                }
            }
        }
    }

    impl Drop for RWorker {
        fn drop(&mut self) {
            self.control.shutdown();
        }
    }

    impl WorkerControl {
        pub fn shutdown(&self) {
            if self.has_exited() {
                return;
            }
            let _ = self.writer.send(&ServerMessage::Shutdown);
            let started = Instant::now();
            while started.elapsed() < SHUTDOWN_GRACE {
                if self.has_exited() {
                    return;
                }
                thread::sleep(CHILD_POLL_INTERVAL);
            }
            if let Ok(mut child) = self.child.lock() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }

        fn has_exited(&self) -> bool {
            self.child
                .lock()
                .is_ok_and(|mut child| matches!(child.try_wait(), Ok(Some(_))))
        }
    }

    pub fn bootstrap() -> Result<(), Box<dyn Error>> {
        // R discovery executes user-selected code from PATH or R_HOME, so it
        // happens only after sandbox-exec has established the worker boundary.
        // Discovery children must not inherit the sideband reserved for the
        // following self-exec.
        sideband::set_inherited_close_on_exec(true)?;
        let r_home = harp::command::r_home_setup()?;
        let executable = std::env::current_exe()?;
        let mut command = Command::new(executable);
        configure_r_environment(&mut command, &r_home).map_err(io::Error::other)?;
        command.arg("__worker");

        sideband::set_inherited_close_on_exec(false)?;
        let error = command.exec();
        Err(io::Error::new(
            error.kind(),
            format!("failed to enter the embedded R worker: {error}"),
        )
        .into())
    }

    pub fn run() -> Result<(), Box<dyn Error>> {
        let (reader, writer) = sideband::connect_from_env()?;
        WORKER_READER
            .set(Mutex::new(reader))
            .map_err(|_| io::Error::other("R worker sideband reader was already initialized"))?;
        WORKER_WRITER
            .set(writer.clone())
            .map_err(|_| io::Error::other("R worker sideband writer was already initialized"))?;
        initialize_r()?;
        writer.send(&WorkerMessage::Ready)?;

        loop {
            match receive_server_message()? {
                ServerMessage::Evaluate { r } => {
                    clear_interactive_input();
                    let message = match evaluate_r(&r) {
                        Ok(()) => WorkerMessage::Completed,
                        Err(error) => classify_r_error(error),
                    };
                    clear_interactive_input();
                    if WORKER_SHUTDOWN.load(Ordering::SeqCst) {
                        return Ok(());
                    }
                    if let Some(message) = take_worker_failure() {
                        writer.send(&WorkerMessage::Fatal { message })?;
                        return Ok(());
                    }
                    writer.send(&message)?;
                }
                ServerMessage::Input { .. } => {
                    writer.send(&WorkerMessage::Fatal {
                        message: "R worker received stdin outside ReadConsole".to_string(),
                    })?;
                    return Ok(());
                }
                ServerMessage::Shutdown => return Ok(()),
            }
        }
    }

    fn initialize_r() -> Result<(), Box<dyn Error>> {
        let r_home = std::env::var_os("R_HOME")
            .map(std::path::PathBuf::from)
            .ok_or_else(|| io::Error::other("R worker bootstrap did not set R_HOME"))?;
        let libraries = harp::library::RLibraries::from_r_home_path(&r_home);
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
            libr::setup_Rmainloop();
        }

        libraries.initialize_post_setup_r();
        unsafe {
            harp::CONSOLE_THREAD_ID = Some(thread::current().id());
        }
        harp::routines::r_register_routines();
        harp::initialize();
        initialize_traceback_capture()?;
        initialize_r_repl()?;
        Ok(())
    }

    fn initialize_traceback_capture() -> harp::Result<()> {
        let function = harp::parse_eval_base(
            r#"
function() {
    calls <- sys.calls()
    count <- length(calls)
    # Remove the three error-handling calls added around user evaluation.
    if (count > 2L) {
        calls <- calls[-seq.int(count - 2L, count)]
    }
    as.pairlist(rev(calls))
}
"#,
        )?;
        unsafe {
            let call = libr::Rf_protect(libr::Rf_lang1(function.sexp));
            libr::R_PreserveObject(call);
            libr::Rf_unprotect(1);
            TRACEBACK_CALL = Some(call);
        }
        Ok(())
    }

    fn initialize_r_repl() -> Result<(), Box<dyn Error>> {
        type Init = unsafe extern "C-unwind" fn();
        type DoOne = unsafe extern "C-unwind" fn() -> c_int;

        let library = libloading::os::unix::Library::this();
        let init = unsafe { *library.get::<Init>(b"R_ReplDLLinit\0")? };
        let do_one = unsafe { *library.get::<DoOne>(b"R_ReplDLLdo1\0")? };
        R_REPL_INIT
            .set(init)
            .map_err(|_| io::Error::other("R REPL was already initialized"))?;
        R_REPL_DO_ONE
            .set(do_one)
            .map_err(|_| io::Error::other("R REPL was already initialized"))?;
        unsafe {
            init();
        }
        Ok(())
    }

    fn configure_r_environment(command: &mut Command, r_home: &Path) -> Result<(), String> {
        command.env("R_HOME", r_home.as_os_str());

        #[cfg(target_os = "linux")]
        prepend_loader_path(command, "LD_LIBRARY_PATH", r_home)?;
        #[cfg(target_os = "macos")]
        prepend_loader_path(command, "DYLD_LIBRARY_PATH", r_home)?;

        Ok(())
    }

    #[cfg(any(target_os = "linux", target_os = "macos"))]
    fn prepend_loader_path(command: &mut Command, name: &str, r_home: &Path) -> Result<(), String> {
        // The dynamic loader reads these paths at process launch, before Harp opens libR.
        let mut paths = vec![r_home.join("lib")];
        if let Some(existing) = std::env::var_os(name) {
            paths.extend(std::env::split_paths(&existing));
        }
        let paths = std::env::join_paths(paths)
            .map_err(|error| format!("failed to construct {name}: {error}"))?;
        command.env(name, paths);
        Ok(())
    }

    fn restore_loader_environment(command: &mut crate::sandbox::WorkerCommand) {
        // sandbox-exec strips DYLD_* variables. Restore an existing search
        // path inside the sandbox before the bootstrap prefixes R's library.
        #[cfg(target_os = "macos")]
        if let Some(existing) = std::env::var_os("DYLD_LIBRARY_PATH") {
            command.env("DYLD_LIBRARY_PATH", existing);
        }
    }

    fn evaluate_r(code: &str) -> harp::Result<()> {
        let expressions = harp::parse_exprs(code)?;

        for index in 0..expressions.length() {
            let expression = harp::list_get(expressions.sexp, index);
            INTERACTIVE_READ.store(false, Ordering::SeqCst);
            let (value, visible) = evaluate_expression(expression)?;
            if INTERACTIVE_READ.swap(false, Ordering::SeqCst) {
                reset_r_repl();
            }
            let value = harp::RObject::from(value);
            finish_top_level_expression(&value, visible)?;
        }

        Ok(())
    }

    #[repr(C)]
    struct EvalBodyData {
        expression: libr::SEXP,
        environment: libr::SEXP,
    }

    unsafe extern "C-unwind" fn eval_body(data: *mut c_void) -> libr::SEXP {
        let data = unsafe { &*(data.cast::<EvalBodyData>()) };
        unsafe { libr::Rf_eval(data.expression, data.environment) }
    }

    unsafe extern "C-unwind" fn save_traceback(
        _error: libr::SEXP,
        _data: *mut c_void,
    ) -> libr::SEXP {
        let call = unsafe { TRACEBACK_CALL.unwrap_unchecked() };
        unsafe {
            let traceback = libr::Rf_protect(libr::Rf_eval(call, harp::R_ENVS.base));
            let symbol = libr::Rf_install(c".Traceback".as_ptr());
            libr::SETCDR(symbol, traceback);
            libr::Rf_unprotect(1);
        }
        harp::r_null()
    }

    fn evaluate_expression(expression: libr::SEXP) -> harp::Result<(libr::SEXP, bool)> {
        let mut data = EvalBodyData {
            expression,
            environment: harp::R_ENVS.global,
        };
        harp::try_catch(|| unsafe {
            libr::set(libr::R_Visible, libr::Rboolean_FALSE);
            let value = harp::exec::with_calling_error_handler(
                eval_body,
                (&mut data as *mut EvalBodyData).cast(),
                save_traceback,
                std::ptr::null_mut(),
            );
            let visible = libr::get(libr::R_Visible) == libr::Rboolean_TRUE;
            (value, visible)
        })
    }

    fn finish_top_level_expression(value: &harp::RObject, visible: bool) -> harp::Result<()> {
        // The cell is already parsed and evaluated. This proxy asks R's native
        // top-level loop only to autoprint and run warning and task-callback bookkeeping.
        let (proxy_name, proxy) = unused_proxy_name();
        harp::try_catch(|| unsafe {
            libr::Rf_defineVar(proxy, value.sexp, harp::R_ENVS.global);
        })?;
        let input = if visible {
            format!("base::get(\"{proxy_name}\", envir = base::globalenv(), inherits = FALSE)\n")
        } else {
            format!(
                "base::invisible(base::get(\"{proxy_name}\", envir = base::globalenv(), inherits = FALSE))\n"
            )
        };
        let mut pending = R_REPL_INPUT
            .lock()
            .expect("R REPL input lock should not be poisoned");
        assert!(
            pending.replace(input.into_bytes()).is_none(),
            "R REPL input should be consumed before another expression"
        );
        drop(pending);

        let do_one = *R_REPL_DO_ONE
            .get()
            .expect("R REPL should be initialized before evaluation");
        let status = harp::top_level_exec(|| unsafe { do_one() });
        if status.is_err() {
            reset_r_repl();
        }
        unsafe {
            libr::R_removeVarFromFrame(proxy, harp::R_ENVS.global);
        }
        let status = status?;
        assert_eq!(status, 1, "fixed R top-level proxy should be complete");
        Ok(())
    }

    fn reset_r_repl() {
        let init = *R_REPL_INIT
            .get()
            .expect("R REPL should be initialized before evaluation");
        unsafe {
            init();
        }
    }

    fn unused_proxy_name() -> (String, libr::SEXP) {
        loop {
            let counter = R_REPL_PROXY_COUNTER.fetch_add(1, Ordering::Relaxed);
            let name = format!("..mcp_console_value_{}_{}..", std::process::id(), counter);
            let c_name = CString::new(name.as_str()).expect("R proxy name should not contain NUL");
            let symbol = unsafe { libr::Rf_install(c_name.as_ptr()) };
            if unsafe {
                libr::R_existsVarInFrame(harp::R_ENVS.global, symbol) == libr::Rboolean_FALSE
            } {
                return (name, symbol);
            }
        }
    }

    fn classify_r_error(error: harp::Error) -> WorkerMessage {
        match error {
            harp::Error::ParseError { message, .. } | harp::Error::ParseSyntaxError { message } => {
                WorkerMessage::LanguageError { message }
            }
            harp::Error::TryCatchError(error) => WorkerMessage::LanguageError {
                message: error.message,
            },
            // R's native top-level loop already printed autoprint failures.
            harp::Error::TopLevelExecError { .. } => WorkerMessage::LanguageError {
                message: String::new(),
            },
            error => WorkerMessage::Fatal {
                message: format!("R worker evaluation failed internally: {error}"),
            },
        }
    }

    fn receive_server_message() -> io::Result<ServerMessage> {
        WORKER_READER
            .get()
            .ok_or_else(|| io::Error::other("R worker sideband reader is unavailable"))?
            .lock()
            .map_err(|_| io::Error::other("R worker sideband reader lock poisoned"))?
            .receive()
    }

    fn clear_interactive_input() {
        INTERACTIVE_INPUT
            .lock()
            .expect("R interactive input lock should not be poisoned")
            .clear();
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

    fn append_text(output: &mut Vec<u8>, text: &str) {
        if text.is_empty() {
            return;
        }
        output.extend_from_slice(text.as_bytes());
    }

    fn append_marker(output: &mut Vec<u8>, marker: &str) {
        if !output.is_empty() && !output.ends_with(b"\n") {
            output.push(b'\n');
        }
        output.extend_from_slice(marker.as_bytes());
    }

    fn append_language_error(output: &mut Vec<u8>, message: &str) {
        if message.is_empty() {
            if output.is_empty() {
                output.extend_from_slice(b"Error: R top-level evaluation failed\n");
            }
            return;
        }
        if !output.is_empty() && !output.ends_with(b"\n") {
            output.push(b'\n');
        }
        output.extend_from_slice(b"Error: ");
        output.extend_from_slice(message.trim_end().as_bytes());
        output.push(b'\n');
    }

    fn emit_output(bytes: &[u8]) {
        if !sideband::available_in_process() {
            return;
        }
        let Some(writer) = WORKER_WRITER.get() else {
            return;
        };
        for chunk in bytes.chunks(OUTPUT_CHUNK_BYTES) {
            writer
                .send(&WorkerMessage::Output {
                    data_b64: STANDARD.encode(chunk),
                })
                .expect("R console output should be sent over the worker sideband");
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
        let internal_input = R_REPL_INPUT
            .lock()
            .ok()
            .and_then(|mut pending| pending.take());
        if let Some(input) = internal_input {
            return write_console_input(buf, buflen, &input);
        }

        if !sideband::available_in_process() {
            return console_eof(buf);
        }
        INTERACTIVE_READ.store(true, Ordering::SeqCst);

        let prompt = if prompt.is_null() {
            String::new()
        } else {
            unsafe { CStr::from_ptr(prompt) }
                .to_string_lossy()
                .into_owned()
        };
        if let Some(input) = take_complete_console_input((buflen as usize) - 1) {
            return write_console_input(buf, buflen, &input);
        }
        if let Err(error) = WORKER_WRITER
            .get()
            .expect("R worker sideband writer should be initialized")
            .send(&WorkerMessage::InputRequested {
                prompt: prompt.clone(),
            })
        {
            record_worker_failure(format!(
                "R worker failed to report an input request: {error}"
            ));
            return console_eof(buf);
        }

        loop {
            if let Some(input) = take_complete_console_input((buflen as usize) - 1) {
                return write_console_input(buf, buflen, &input);
            }

            match receive_server_message() {
                Ok(ServerMessage::Input { stdin }) => {
                    if stdin.contains('\0') {
                        record_worker_failure("R worker stdin contained NUL".to_string());
                        return console_eof(buf);
                    }
                    INTERACTIVE_INPUT
                        .lock()
                        .expect("R interactive input lock should not be poisoned")
                        .extend_from_slice(stdin.as_bytes());
                    if !interactive_input_has_line()
                        && let Err(error) = WORKER_WRITER
                            .get()
                            .expect("R worker sideband writer should be initialized")
                            .send(&WorkerMessage::InputPending {
                                prompt: prompt.clone(),
                            })
                    {
                        record_worker_failure(format!(
                            "R worker failed to report pending input: {error}"
                        ));
                        return console_eof(buf);
                    }
                }
                Ok(ServerMessage::Shutdown) => {
                    WORKER_SHUTDOWN.store(true, Ordering::SeqCst);
                    return console_eof(buf);
                }
                Ok(ServerMessage::Evaluate { .. }) => {
                    record_worker_failure(
                        "R worker received code while waiting for stdin".to_string(),
                    );
                    return console_eof(buf);
                }
                Err(error) => {
                    record_worker_failure(format!(
                        "R worker sideband read failed while waiting for stdin: {error}"
                    ));
                    return console_eof(buf);
                }
            }
        }
    }

    fn interactive_input_has_line() -> bool {
        INTERACTIVE_INPUT
            .lock()
            .expect("R interactive input lock should not be poisoned")
            .contains(&b'\n')
    }

    fn take_complete_console_input(max: usize) -> Option<Vec<u8>> {
        let mut input = INTERACTIVE_INPUT
            .lock()
            .expect("R interactive input lock should not be poisoned");
        let newline = input.iter().position(|byte| *byte == b'\n')?;
        let mut split = (newline + 1).min(max);
        while split > 0
            && std::str::from_utf8(&input).is_ok_and(|text| !text.is_char_boundary(split))
        {
            split -= 1;
        }
        assert!(split > 0, "R console buffer is too small for UTF-8 input");
        Some(input.drain(..split).collect())
    }

    fn write_console_input(buf: *mut c_uchar, buflen: c_int, input: &[u8]) -> c_int {
        if input.len() >= buflen as usize {
            return console_eof(buf);
        }
        unsafe {
            // Browser commands such as `c` return before R's nested REPL resets
            // visibility. Reset it at the same input boundary so a prior
            // browser expression is not spuriously auto-printed as the cell result.
            libr::set(libr::R_Visible, libr::Rboolean_FALSE);
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

    fn drain(stream: Option<impl Read + Send + 'static>) {
        if let Some(mut stream) = stream {
            thread::spawn(move || {
                let _ = io::copy(&mut stream, &mut io::sink());
            });
        }
    }

    fn worker_io_error(child: &Mutex<Child>, error: io::Error) -> String {
        let started = Instant::now();
        while started.elapsed() < Duration::from_millis(250) {
            let status = match child.lock() {
                Ok(mut child) => child.try_wait(),
                Err(_) => return format!("R worker status lock poisoned: {error}"),
            };
            match status {
                Ok(Some(status)) => return format!("R worker exited with {status}: {error}"),
                Ok(None) => thread::sleep(Duration::from_millis(10)),
                Err(wait_error) => {
                    return format!("R worker status failed: {wait_error}; {error}");
                }
            }
        }
        format!("R worker sideband failed: {error}")
    }
}

#[cfg(target_family = "unix")]
pub use unix::{RWorker, WorkerControl, bootstrap, run};

#[cfg(not(target_family = "unix"))]
pub struct RWorker;

#[cfg(not(target_family = "unix"))]
#[derive(Clone)]
pub struct WorkerControl;

#[cfg(not(target_family = "unix"))]
impl RWorker {
    pub fn start(_on_started: impl FnOnce(WorkerControl)) -> Result<Self, String> {
        Err("sandboxed R sessions are not supported on this operating system".to_string())
    }

    pub fn evaluate(&mut self, _r: String) -> Result<Boundary, String> {
        Err("sandboxed R sessions are not supported on this operating system".to_string())
    }

    pub fn provide_input(&mut self, _stdin: String) -> Result<Boundary, String> {
        Err("sandboxed R sessions are not supported on this operating system".to_string())
    }

    pub fn control(&self) -> WorkerControl {
        WorkerControl
    }
}

#[cfg(not(target_family = "unix"))]
impl WorkerControl {
    pub fn shutdown(&self) {}
}

#[cfg(not(target_family = "unix"))]
pub fn run() -> Result<(), Box<dyn std::error::Error>> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "embedded R workers are supported only on Unix",
    )
    .into())
}

#[cfg(not(target_family = "unix"))]
pub fn bootstrap() -> Result<(), Box<dyn std::error::Error>> {
    run()
}
