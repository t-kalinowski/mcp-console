#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub enum Boundary {
    Complete(String),
    Input(String),
}

#[cfg(target_os = "macos")]
mod unix {
    use std::error::Error;
    use std::ffi::{CStr, CString, OsStr, OsString, c_char, c_int, c_uchar};
    use std::io::{self, Read};
    use std::os::unix::process::CommandExt;
    use std::path::Path;
    use std::process::{Command, Stdio};
    use std::sync::atomic::{AtomicBool, Ordering};
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
    static R_REPL_INIT: OnceLock<ReplInit> = OnceLock::new();
    static R_REPL_DO_ONE: OnceLock<ReplDoOne> = OnceLock::new();
    static CELL_SOURCE: Mutex<Option<CellSource>> = Mutex::new(None);
    static INTERACTIVE_INPUT: Mutex<Vec<u8>> = Mutex::new(Vec::new());
    static WORKER_FAILURE: Mutex<Option<String>> = Mutex::new(None);
    static WORKER_SHUTDOWN: AtomicBool = AtomicBool::new(false);
    static EVALUATION_STARTED: AtomicBool = AtomicBool::new(false);

    type ReplInit = unsafe extern "C-unwind" fn();
    type ReplDoOne = unsafe extern "C-unwind" fn() -> c_int;

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
    }

    #[derive(Clone)]
    pub struct WorkerControl {
        writer: sideband::Writer,
        child: Arc<Mutex<crate::sandbox::SandboxedChild>>,
    }

    impl RWorker {
        pub fn start(on_started: impl FnOnce(WorkerControl)) -> Result<Self, String> {
            let (mut reader, writer, child_fds) = sideband::bind()
                .map_err(|error| format!("failed to create R worker sideband: {error}"))?;
            let executable = std::env::current_exe()
                .map_err(|error| format!("failed to locate mcp-console: {error}"))?;
            let mut command = crate::sandbox::SandboxedCommand::new(OsStr::new("/usr/bin/env"))?;
            restore_loader_environment(&mut command);
            command
                .arg(executable.as_os_str())
                .arg("__worker_bootstrap")
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            child_fds.configure(&mut command);

            let mut child = command
                .spawn()
                .map_err(|error| format!("failed to launch sandboxed R worker: {error}"))?;
            drop(child_fds);
            drain(child.take_stdout());
            drain(child.take_stderr());
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

            Ok(Self { reader, control })
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
                    let message = if r.contains('\0') {
                        WorkerMessage::LanguageError {
                            message: "R source cannot contain NUL".to_string(),
                        }
                    } else {
                        set_cell_source(r);
                        let status = run_repl_cell();
                        clear_cell_source();
                        match status {
                            0 => WorkerMessage::LanguageError {
                                message: String::new(),
                            },
                            1 => WorkerMessage::Completed,
                            2 => WorkerMessage::LanguageError {
                                message: "Incomplete code".to_string(),
                            },
                            status => WorkerMessage::Fatal {
                                message: format!(
                                    "R worker received unexpected DLL REPL status {status}"
                                ),
                            },
                        }
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
        R_REPL_INIT
            .set(init)
            .map_err(|_| io::Error::other("R REPL was already initialized"))?;
        R_REPL_DO_ONE
            .set(do_one)
            .map_err(|_| io::Error::other("R REPL was already initialized"))?;
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

    fn restore_loader_environment(command: &mut crate::sandbox::SandboxedCommand) {
        // sandbox-exec strips DYLD_* variables. The worker-only env process
        // restores the caller's search path before the bootstrap prefixes R's.
        if let Some(existing) = std::env::var_os("DYLD_LIBRARY_PATH") {
            let mut assignment = OsString::from("DYLD_LIBRARY_PATH=");
            assignment.push(existing);
            command.arg(assignment);
        }
    }

    fn run_repl_cell() -> c_int {
        let init = *R_REPL_INIT
            .get()
            .expect("R REPL should be initialized before evaluation");
        let do_one = *R_REPL_DO_ONE
            .get()
            .expect("R REPL should be initialized before evaluation");
        unsafe { mcp_r_repl_run_cell(init, do_one, before_repl_iteration) }
    }

    extern "C" fn before_repl_iteration() {
        EVALUATION_STARTED.store(false, Ordering::SeqCst);
    }

    extern "C-unwind" fn r_busy(which: c_int) {
        // This is a latch for the outer DLL iteration. A nested browser REPL
        // calls Busy(0) before requesting input, so only Busy(1) changes it.
        // Parse-error handlers run before Busy(1); interactive reads from such
        // handlers are unsupported because the public DLL API exposes no state
        // that distinguishes them from cell-source reads.
        if which != 0 {
            EVALUATION_STARTED.store(true, Ordering::SeqCst);
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
            if let Err(error) = writer.send(&WorkerMessage::Output {
                data_b64: STANDARD.encode(chunk),
            }) {
                record_worker_failure(format!("R worker failed to send console output: {error}"));
                return;
            }
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

        if !sideband::available_in_process() {
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

    fn worker_io_error(child: &Mutex<crate::sandbox::SandboxedChild>, error: io::Error) -> String {
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

#[cfg(target_os = "macos")]
pub use unix::{RWorker, WorkerControl, bootstrap, run};

#[cfg(not(target_os = "macos"))]
pub struct RWorker;

#[cfg(not(target_os = "macos"))]
#[derive(Clone)]
pub struct WorkerControl;

#[cfg(not(target_os = "macos"))]
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

#[cfg(not(target_os = "macos"))]
impl Drop for RWorker {
    fn drop(&mut self) {}
}

#[cfg(not(target_os = "macos"))]
impl WorkerControl {
    pub fn shutdown(&self) {}
}

#[cfg(not(target_os = "macos"))]
pub fn run() -> Result<(), Box<dyn std::error::Error>> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "embedded R workers are supported only on Unix",
    )
    .into())
}

#[cfg(not(target_os = "macos"))]
pub fn bootstrap() -> Result<(), Box<dyn std::error::Error>> {
    run()
}
