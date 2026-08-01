#[cfg(target_os = "macos")]
mod platform {
    use std::error::Error;
    use std::ffi::{CStr, CString, c_char, c_int, c_uchar};
    use std::io;
    use std::os::unix::process::CommandExt as _;
    use std::process::Command;
    use std::sync::{Mutex, OnceLock};
    use std::thread;

    use crate::worker_protocol::{ServerMessage, WorkerMessage};

    static R_MAIN_ARGS: OnceLock<Vec<CString>> = OnceLock::new();
    static WORKER_WRITER: OnceLock<crate::sideband::Writer> = OnceLock::new();
    static OUTPUT_FAILURE: Mutex<Option<String>> = Mutex::new(None);

    pub(crate) fn bootstrap() -> Result<(), Box<dyn Error>> {
        // R discovery executes user-selected code from PATH or R_HOME, so it
        // happens only after sandbox-exec has established the worker boundary.
        crate::sideband::set_inherited_close_on_exec(true)?;
        let r_home = harp::command::r_home_setup()?;
        let executable = std::env::current_exe()?;
        let mut command = Command::new(executable);
        configure_r_environment(&mut command, &r_home);
        command.args(["worker", "run"]);

        crate::sideband::set_inherited_close_on_exec(false)?;
        let error = command.exec();
        Err(io::Error::new(
            error.kind(),
            format!("failed to enter the embedded R worker: {error}"),
        )
        .into())
    }

    pub(crate) fn run() -> Result<(), Box<dyn Error>> {
        // SAFETY: pthread_main_np has no preconditions.
        if unsafe { libc::pthread_main_np() } != 1 {
            return Err(io::Error::other("R worker must run on the process main thread").into());
        }
        let (mut reader, writer) = crate::sideband::connect_from_env()?;
        initialize_r()?;
        WORKER_WRITER
            .set(writer.clone())
            .map_err(|_| io::Error::other("R worker sideband was already initialized"))?;
        writer.send(&WorkerMessage::Ready)?;

        loop {
            match reader.receive::<ServerMessage>()? {
                ServerMessage::Evaluate { r } => {
                    if let Err(error) = evaluate_r(&r) {
                        let message = language_error_message(error)?;
                        emit_output(format!("Error: {message}\n").as_bytes());
                    }
                    if let Some(message) = take_output_failure() {
                        return Err(io::Error::other(message).into());
                    }
                    writer.send(&WorkerMessage::Completed)?;
                }
                ServerMessage::Shutdown => return Ok(()),
            }
        }
    }

    fn configure_r_environment(command: &mut Command, r_home: &std::path::Path) {
        command
            .env("R_HOME", r_home)
            .env("DYLD_LIBRARY_PATH", r_home.join("lib"));
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
        Ok(())
    }

    fn evaluate_r(source: &str) -> harp::Result<()> {
        let expressions = harp::parse_exprs(source)?;
        for index in 0..expressions.length() {
            let expression = harp::list_get(expressions.sexp, index);
            let (value, visible) = harp::try_catch(|| unsafe {
                libr::set(libr::R_Visible, libr::Rboolean_FALSE);
                let value = libr::Rf_eval(expression, harp::R_ENVS.global);
                let visible = libr::get(libr::R_Visible) == libr::Rboolean_TRUE;
                (value, visible)
            })?;
            let value = harp::RObject::from(value);
            if visible {
                harp::utils::r_print(&value)?;
            }
        }
        Ok(())
    }

    fn language_error_message(error: harp::Error) -> Result<String, harp::Error> {
        let message = match error {
            harp::Error::ParseError { message, .. } | harp::Error::ParseSyntaxError { message } => {
                message
            }
            harp::Error::TryCatchError(error) => error.message,
            error => return Err(error),
        };
        let message = message.trim_end();
        Ok(if message.is_empty() {
            "R evaluation failed".to_string()
        } else {
            message.to_string()
        })
    }

    fn emit_output(bytes: &[u8]) {
        if !crate::sideband::available_in_process() {
            return;
        }
        let Some(writer) = WORKER_WRITER.get() else {
            return;
        };
        if OUTPUT_FAILURE.lock().is_ok_and(|failure| failure.is_some()) {
            return;
        }
        if let Err(error) = writer.send(&WorkerMessage::Output {
            data: String::from_utf8_lossy(bytes).into_owned(),
        }) && let Ok(mut failure) = OUTPUT_FAILURE.lock()
        {
            *failure = Some(format!("R console output failed: {error}"));
        }
    }

    fn take_output_failure() -> Option<String> {
        OUTPUT_FAILURE
            .lock()
            .ok()
            .and_then(|mut failure| failure.take())
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
        if !buf.is_null() && buflen > 0 {
            unsafe {
                *buf = 0;
            }
        }
        0
    }
}

#[cfg(target_os = "macos")]
pub(crate) use platform::{bootstrap, run};

#[cfg(not(target_os = "macos"))]
pub(crate) fn bootstrap() -> Result<(), Box<dyn std::error::Error>> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "embedded R workers are supported only on macOS",
    )
    .into())
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn run() -> Result<(), Box<dyn std::error::Error>> {
    bootstrap()
}
