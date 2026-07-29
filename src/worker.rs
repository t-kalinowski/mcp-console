#[cfg(target_family = "unix")]
mod unix {
    use std::error::Error;
    use std::ffi::{CStr, CString, c_char, c_int, c_uchar};
    use std::io::{self, Read};
    use std::process::{Child, Command, Stdio};
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::{Mutex, OnceLock};
    use std::thread;

    use base64::Engine as _;
    use base64::engine::general_purpose::STANDARD;
    use serde::{Deserialize, Serialize};

    use crate::sideband;

    const OUTPUT_CHUNK_BYTES: usize = 8 * 1024;

    static R_MAIN_ARGS: OnceLock<Vec<CString>> = OnceLock::new();
    static WORKER_WRITER: OnceLock<sideband::Writer> = OnceLock::new();
    static R_REPL_INIT: OnceLock<unsafe extern "C-unwind" fn()> = OnceLock::new();
    static R_REPL_DO_ONE: OnceLock<unsafe extern "C-unwind" fn() -> c_int> = OnceLock::new();
    static R_REPL_INPUT: Mutex<Option<Vec<u8>>> = Mutex::new(None);
    static R_REPL_PROXY_COUNTER: AtomicU64 = AtomicU64::new(0);

    #[derive(Serialize, Deserialize)]
    #[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
    enum ServerMessage {
        Evaluate { r: String },
        Shutdown,
    }

    #[derive(Serialize, Deserialize)]
    #[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
    enum WorkerMessage {
        Ready,
        Output { data_b64: String },
        Error { message: String },
    }

    pub struct RWorker {
        reader: sideband::Reader,
        writer: sideband::Writer,
        child: Child,
    }

    impl RWorker {
        pub fn start() -> io::Result<Self> {
            let (mut reader, writer, child_fds) = sideband::bind()?;
            let mut command = Command::new(std::env::current_exe()?);
            configure_r_environment(&mut command)?;
            command
                .arg("__worker")
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            child_fds.configure(&mut command);

            let mut child = command.spawn()?;
            drop(child_fds);
            drain(child.stdout.take());
            drain(child.stderr.take());

            let message = reader
                .receive::<WorkerMessage>()
                .map_err(|error| worker_io_error(&mut child, error))?;
            if !matches!(message, WorkerMessage::Ready) {
                let _ = child.kill();
                let _ = child.wait();
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "R worker did not report readiness",
                ));
            }

            Ok(Self {
                reader,
                writer,
                child,
            })
        }

        pub fn evaluate(&mut self, r: String) -> Result<String, String> {
            self.writer
                .send(&ServerMessage::Evaluate { r })
                .map_err(|error| format!("R worker sideband write failed: {error}"))?;

            let mut output = Vec::new();
            loop {
                let message = self.reader.receive::<WorkerMessage>().map_err(|error| {
                    format!(
                        "R worker sideband read failed: {}",
                        worker_io_error(&mut self.child, error)
                    )
                })?;
                match message {
                    WorkerMessage::Output { data_b64 } => {
                        let bytes = STANDARD
                            .decode(data_b64)
                            .map_err(|error| format!("R worker sent invalid output: {error}"))?;
                        output.extend_from_slice(&bytes);
                    }
                    WorkerMessage::Ready => {
                        return Ok(String::from_utf8_lossy(&output).into_owned());
                    }
                    WorkerMessage::Error { message } => {
                        if message.is_empty() {
                            if output.is_empty() {
                                output.extend_from_slice(b"Error: R top-level evaluation failed\n");
                            }
                        } else {
                            output.extend_from_slice(b"Error: ");
                            output.extend_from_slice(message.trim_end().as_bytes());
                            output.push(b'\n');
                        }
                        return Err(String::from_utf8_lossy(&output).into_owned());
                    }
                }
            }
        }
    }

    impl Drop for RWorker {
        fn drop(&mut self) {
            let _ = self.writer.send(&ServerMessage::Shutdown);
            let _ = self.child.wait();
        }
    }

    pub fn run() -> Result<(), Box<dyn Error>> {
        let (mut reader, writer) = sideband::connect_from_env()?;
        initialize_r()?;
        WORKER_WRITER
            .set(writer.clone())
            .map_err(|_| io::Error::other("R worker sideband was already initialized"))?;
        writer.send(&WorkerMessage::Ready)?;

        loop {
            match reader.receive::<ServerMessage>()? {
                ServerMessage::Evaluate { r } => {
                    let message = match evaluate_r(&r) {
                        Ok(()) => WorkerMessage::Ready,
                        Err(error) => WorkerMessage::Error {
                            message: concise_r_error(error),
                        },
                    };
                    writer.send(&message)?;
                }
                ServerMessage::Shutdown => return Ok(()),
            }
        }
    }

    fn initialize_r() -> Result<(), Box<dyn Error>> {
        let r_home = harp::command::r_home_setup()?;
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
        initialize_r_repl()?;
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

    fn configure_r_environment(command: &mut Command) -> io::Result<()> {
        let r_home = harp::command::r_home_setup().map_err(io::Error::other)?;
        command.env("R_HOME", &r_home);

        #[cfg(target_os = "linux")]
        prepend_loader_path(command, "LD_LIBRARY_PATH", &r_home)?;
        #[cfg(target_os = "macos")]
        prepend_loader_path(command, "DYLD_LIBRARY_PATH", &r_home)?;

        Ok(())
    }

    #[cfg(any(target_os = "linux", target_os = "macos"))]
    fn prepend_loader_path(
        command: &mut Command,
        name: &str,
        r_home: &std::path::Path,
    ) -> io::Result<()> {
        // The dynamic loader reads these paths at process launch, before Harp opens libR.
        let mut paths = vec![r_home.join("lib")];
        if let Some(existing) = std::env::var_os(name) {
            paths.extend(std::env::split_paths(&existing));
        }
        let paths = std::env::join_paths(paths)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
        command.env(name, paths);
        Ok(())
    }

    fn evaluate_r(code: &str) -> harp::Result<()> {
        let expressions = harp::parse_exprs(code)?;

        for index in 0..expressions.length() {
            let expression = harp::list_get(expressions.sexp, index);
            let (value, visible) = harp::try_catch(|| unsafe {
                libr::set(libr::R_Visible, libr::Rboolean_FALSE);
                let value = libr::Rf_eval(expression, harp::R_ENVS.global);
                let visible = libr::get(libr::R_Visible) == libr::Rboolean_TRUE;
                (value, visible)
            })?;
            let value = harp::RObject::from(value);
            finish_top_level_expression(&value, visible)?;
        }

        Ok(())
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
            let init = *R_REPL_INIT
                .get()
                .expect("R REPL should be initialized before evaluation");
            unsafe {
                init();
            }
        }
        unsafe {
            libr::R_removeVarFromFrame(proxy, harp::R_ENVS.global);
        }
        let status = status?;
        assert_eq!(status, 1, "fixed R top-level proxy should be complete");
        Ok(())
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

    fn concise_r_error(error: harp::Error) -> String {
        match error {
            harp::Error::ParseError { message, .. } | harp::Error::ParseSyntaxError { message } => {
                message
            }
            harp::Error::TryCatchError(error) => error.message,
            harp::Error::TopLevelExecError { .. } => String::new(),
            error => error.to_string(),
        }
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
        _prompt: *const c_char,
        buf: *mut c_uchar,
        buflen: c_int,
        _add_history: c_int,
    ) -> c_int {
        if buf.is_null() || buflen <= 0 {
            return 0;
        }
        let input = R_REPL_INPUT
            .lock()
            .ok()
            .and_then(|mut pending| pending.take());
        let Some(input) = input else {
            unsafe {
                *buf = 0;
            }
            return 0;
        };
        if input.len() >= buflen as usize {
            return 0;
        }
        unsafe {
            std::ptr::copy_nonoverlapping(input.as_ptr(), buf, input.len());
            *buf.add(input.len()) = 0;
        }
        1
    }

    fn drain(stream: Option<impl Read + Send + 'static>) {
        if let Some(mut stream) = stream {
            thread::spawn(move || {
                let _ = io::copy(&mut stream, &mut io::sink());
            });
        }
    }

    fn worker_io_error(child: &mut Child, error: io::Error) -> io::Error {
        match child.try_wait() {
            Ok(Some(status)) => io::Error::new(
                error.kind(),
                format!("R worker exited with {status}: {error}"),
            ),
            _ => error,
        }
    }
}

#[cfg(target_family = "unix")]
pub use unix::{RWorker, run};

#[cfg(not(target_family = "unix"))]
pub struct RWorker;

#[cfg(not(target_family = "unix"))]
impl RWorker {
    pub fn start() -> std::io::Result<Self> {
        Err(std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "embedded R workers are supported only on Unix",
        ))
    }

    pub fn evaluate(&mut self, _r: String) -> Result<String, String> {
        Err("embedded R workers are supported only on Unix".to_string())
    }
}

#[cfg(not(target_family = "unix"))]
pub fn run() -> Result<(), Box<dyn std::error::Error>> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "embedded R workers are supported only on Unix",
    )
    .into())
}
