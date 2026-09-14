use std::error::Error;
use std::io;
use std::path::PathBuf;

use super::{core, embedded_r, input, interrupt};
use crate::cell::Language;
use crate::worker_protocol::{ConsoleChannel, ServerMessage, WorkerMessage};

pub(crate) fn run() -> Result<(), Box<dyn Error>> {
    let (reader, writer) = crate::sideband::connect_from_env()?;
    let r_home = embedded_r::discover()?;
    #[cfg(target_os = "linux")]
    if let Some(home) = &r_home {
        reexec_with_r_library_path(home, &reader, &writer)?;
    }
    let temporary = TemporaryDirectory::new()?;
    core::initialize(reader, writer.clone())?;
    interrupt::initialize()?;
    crate::python::configure_worker_environment(&temporary.0)?;
    let mut runtime = Runtime {
        writer,
        python: crate::python::Runtime::initialize()?,
        sql: crate::sql::Bridge::initialize(r_home.is_some())?,
    };
    runtime.writer.send(&WorkerMessage::Ready)?;
    let result = runtime.run();
    crate::python::prepare_process_exit()?;
    result
}

struct Runtime {
    writer: crate::sideband::Writer,
    python: crate::python::Runtime,
    sql: crate::sql::Bridge,
}

impl Runtime {
    fn run(&mut self) -> Result<(), Box<dyn Error>> {
        loop {
            let message = self.wait_for_message()?;
            if !self.handle(message)? {
                return Ok(());
            }
        }
    }

    fn wait_for_message(&self) -> Result<ServerMessage, String> {
        loop {
            if core::is_shutting_down() {
                return Ok(ServerMessage::Shutdown);
            }
            if let Some(message) = core::take_pending_server_message()? {
                return Ok(message);
            }
            let (buffered, fd) = core::sideband_activity()?;
            if buffered {
                return core::receive_server_message();
            }
            if embedded_r::is_active() {
                if embedded_r::wait_for_activity(fd)? {
                    return core::receive_server_message();
                }
                embedded_r::idle()?;
            } else {
                let mut descriptors = [
                    libc::pollfd {
                        fd,
                        events: libc::POLLIN,
                        revents: 0,
                    },
                    libc::pollfd {
                        fd: libc::STDIN_FILENO,
                        events: 0,
                        revents: 0,
                    },
                ];
                let status = interrupt::wait(&mut descriptors)?;
                if status < 0 && io::Error::last_os_error().kind() != io::ErrorKind::Interrupted {
                    return Err(io::Error::last_os_error().to_string());
                }
                if descriptors[0].revents != 0 {
                    return core::receive_server_message();
                }
                if descriptors[1].revents & libc::POLLHUP != 0 {
                    return Ok(ServerMessage::Shutdown);
                }
                interrupt::clear();
            }
            if let Some(message) = core::take_worker_failure() {
                return Err(message);
            }
        }
    }

    fn handle(&mut self, message: ServerMessage) -> Result<bool, Box<dyn Error>> {
        interrupt::clear();
        match message {
            ServerMessage::Evaluate { language, source } => {
                embedded_r::idle()?;
                if core::is_shutting_down() {
                    return Ok(false);
                }
                if let Some(message) = core::take_worker_failure() {
                    return Err(io::Error::other(message).into());
                }
                let result = self.evaluate(language, source);
                interrupt::clear();
                input::finish_console_stdin_operation()?;
                if core::is_shutting_down() {
                    return Ok(false);
                }
                if let Some(message) = core::take_worker_failure().or_else(|| result.err()) {
                    return Err(io::Error::other(message).into());
                }
                embedded_r::idle()?;
                self.writer.send(&WorkerMessage::Completed)?;
            }
            ServerMessage::PreparePython { packages } => {
                let result = self.python.prepare(packages)?;
                if core::is_shutting_down() {
                    return Ok(false);
                }
                if let Some(message) = core::take_worker_failure() {
                    return Err(message.into());
                }
                self.writer.send(&match result {
                    crate::python::PreparationOutcome::Prepared => WorkerMessage::PythonPrepared,
                    crate::python::PreparationOutcome::Failed { message } => {
                        WorkerMessage::PythonPreparationFailed { message }
                    }
                })?;
            }
            ServerMessage::PrepareR { library } => {
                let result = embedded_r::prepare(&library);
                if core::is_shutting_down() {
                    return Ok(false);
                }
                if let Some(message) = core::take_worker_failure() {
                    return Err(message.into());
                }
                self.writer.send(&match result {
                    Ok(crate::r_environment::PreparationOutcome::Prepared { library }) => {
                        WorkerMessage::RPrepared { library }
                    }
                    Ok(crate::r_environment::PreparationOutcome::Failed { message })
                    | Err(message) => WorkerMessage::RPreparationFailed { message },
                })?;
            }
            ServerMessage::Shutdown => return Ok(false),
            _ => return Err("worker received an unexpected resolver response".into()),
        }
        Ok(true)
    }

    fn evaluate(&mut self, language: Language, source: String) -> Result<(), String> {
        if source.contains('\0') {
            let message = match language {
                Language::R => "Error: R source cannot contain NUL\n",
                Language::Python => "SyntaxError: source code string cannot contain null bytes\n",
                Language::Sql => "Error: SQL source cannot contain NUL\n",
            };
            core::emit_output(ConsoleChannel::Diagnostic, message.as_bytes());
            return Ok(());
        }
        if matches!(language, Language::R)
            && let Err(message) = embedded_r::ensure_active()
        {
            core::emit_output(
                ConsoleChannel::Diagnostic,
                format!("Error: {message}\n").as_bytes(),
            );
            return Ok(());
        }
        embedded_r::begin_cell()?;
        let result = match language {
            Language::Python => self.python.evaluate(&source),
            Language::Sql => self.sql.evaluate(&source),
            Language::R => embedded_r::evaluate(source),
        };
        interrupt::clear();
        embedded_r::finish_cell()?;
        result
    }
}

#[cfg(target_os = "linux")]
fn reexec_with_r_library_path(
    home: &std::path::Path,
    reader: &crate::sideband::Reader,
    writer: &crate::sideband::Writer,
) -> Result<(), Box<dyn Error>> {
    use std::os::unix::process::CommandExt;
    let library = home.join("lib");
    let mut paths: Vec<_> = std::env::var_os("LD_LIBRARY_PATH")
        .map(|value| std::env::split_paths(&value).collect())
        .unwrap_or_default();
    if paths.first() == Some(&library) {
        return Ok(());
    }
    paths.insert(0, library);
    let mut command = std::process::Command::new(std::env::current_exe()?);
    command
        .args(std::env::args_os().skip(1))
        .env("LD_LIBRARY_PATH", std::env::join_paths(paths)?);
    crate::sideband::configure_exec(reader, writer, &mut command)?;
    Err(command.exec().into())
}

struct TemporaryDirectory(PathBuf);

impl TemporaryDirectory {
    fn new() -> io::Result<Self> {
        use std::os::unix::ffi::{OsStrExt, OsStringExt};
        let template = std::env::temp_dir().join("mcp-console-worker-XXXXXX");
        let mut template = template.as_os_str().as_bytes().to_vec();
        template.push(0);
        if unsafe { libc::mkdtemp(template.as_mut_ptr().cast()) }.is_null() {
            return Err(io::Error::last_os_error());
        }
        template.pop();
        Ok(Self(std::ffi::OsString::from_vec(template).into()))
    }
}

impl Drop for TemporaryDirectory {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}
