#[cfg(target_os = "macos")]
mod platform {
    use std::error::Error;
    use std::ffi::{CStr, CString, c_char, c_int, c_uchar};
    use std::io;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Mutex, OnceLock};
    use std::thread;

    use crate::worker_protocol::{ServerMessage, WorkerMessage};

    static R_MAIN_ARGS: OnceLock<Vec<CString>> = OnceLock::new();
    static WORKER_READER: OnceLock<Mutex<crate::sideband::Reader>> = OnceLock::new();
    static WORKER_WRITER: OnceLock<crate::sideband::Writer> = OnceLock::new();
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

    pub(crate) fn run() -> Result<(), Box<dyn Error>> {
        // SAFETY: pthread_main_np has no preconditions.
        if unsafe { libc::pthread_main_np() } != 1 {
            return Err(io::Error::other("R worker must run on the process main thread").into());
        }

        let (reader, writer) = crate::sideband::connect_from_env()?;
        let r_home = harp::command::r_home_setup()?;
        initialize_r(&r_home)?;
        WORKER_READER
            .set(Mutex::new(reader))
            .map_err(|_| io::Error::other("R worker sideband reader was already initialized"))?;
        WORKER_WRITER
            .set(writer.clone())
            .map_err(|_| io::Error::other("R worker sideband writer was already initialized"))?;
        writer.send(&WorkerMessage::Ready)?;

        loop {
            match receive_server_message()? {
                ServerMessage::Evaluate { r } => {
                    clear_interactive_input();
                    let result = evaluate_cell(r);
                    clear_interactive_input();

                    if WORKER_SHUTDOWN.load(Ordering::SeqCst) {
                        return Ok(());
                    }
                    if let Some(message) = take_worker_failure().or_else(|| result.err()) {
                        writer.send(&WorkerMessage::Fatal { message })?;
                        return Ok(());
                    }
                    writer.send(&WorkerMessage::Completed)?;
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

    fn evaluate_cell(r: String) -> Result<(), String> {
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
        R_REPL_INIT
            .set(init)
            .map_err(|_| io::Error::other("R REPL was already initialized"))?;
        R_REPL_DO_ONE
            .set(do_one)
            .map_err(|_| io::Error::other("R REPL was already initialized"))?;
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

    fn emit_output(bytes: &[u8]) {
        if !crate::sideband::available_in_process() {
            return;
        }
        let Some(writer) = WORKER_WRITER.get() else {
            return;
        };
        if let Err(error) = writer.send(&WorkerMessage::Output {
            data: String::from_utf8_lossy(bytes).into_owned(),
        }) {
            record_worker_failure(format!("R worker failed to send console output: {error}"));
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
                            .send(&WorkerMessage::InputRequested {
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
