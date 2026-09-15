use std::error::Error;
use std::io;

use super::core::{emit_output, take_pending_server_message, take_worker_failure};
use super::input::finish_console_stdin_operation;
use super::{core, embedded_r, interrupt};
use crate::cell::{Cell, Language};
use crate::worker_protocol::{ConsoleChannel, ServerMessage, WorkerMessage};

struct Runtime {
    writer: crate::sideband::Writer,
    r: embedded_r::Runtime,
    python: crate::python::Runtime,
    sql: crate::sql::Bridge,
}

pub(crate) fn run() -> Result<(), Box<dyn Error>> {
    let (reader, writer) = crate::sideband::connect_from_env()?;
    let r_home = harp::command::r_home_setup()?;
    #[cfg(target_os = "linux")]
    reexec_with_r_library_path(&r_home, &reader, &writer)?;
    embedded_r::initialize_r(&r_home)?;
    crate::python::configure_worker_environment(&embedded_r::Runtime::temporary_directory()?)?;
    core::initialize(reader, writer.clone())?;
    interrupt::initialize()?;
    let r = embedded_r::Runtime::initialize()?;
    let python = crate::python::Runtime::initialize()?;
    let sql = crate::sql::Bridge::initialize()?;
    writer.send(&WorkerMessage::Ready)?;

    let mut runtime = Runtime {
        writer,
        r,
        python,
        sql,
    };
    let result = runtime.run();
    crate::python::prepare_process_exit()?;
    result
}

#[cfg(target_os = "linux")]
fn reexec_with_r_library_path(
    r_home: &std::path::Path,
    reader: &crate::sideband::Reader,
    writer: &crate::sideband::Writer,
) -> Result<(), Box<dyn Error>> {
    use std::os::unix::process::CommandExt;

    let library = r_home.join("lib");
    let mut paths: Vec<_> = std::env::var_os("LD_LIBRARY_PATH")
        .map(|value| std::env::split_paths(&value).collect())
        .unwrap_or_default();
    if paths.first() == Some(&library) {
        return Ok(());
    }
    paths.insert(0, library);
    // The ELF loader reads LD_LIBRARY_PATH at exec, before native R packages
    // need to resolve libR.so and its companion libraries.
    let mut command = std::process::Command::new(std::env::current_exe()?);
    command
        .args(std::env::args_os().skip(1))
        .env("LD_LIBRARY_PATH", std::env::join_paths(paths)?);
    crate::sideband::configure_exec(reader, writer, &mut command)?;
    Err(command.exec().into())
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
            if core::is_shutting_down() {
                return Ok(ServerMessage::Shutdown);
            }
            if let Some(message) = take_pending_server_message()? {
                return Ok(message);
            }

            let (buffered, sideband_fd) = core::sideband_activity()?;
            if buffered {
                return core::receive_server_message();
            }
            if embedded_r::wait_for_activity(sideband_fd)? {
                return core::receive_server_message();
            }

            self.r.idle()?;
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
            self.r.idle().map_err(io::Error::other)?;
            if core::is_shutting_down() {
                return Ok(false);
            }
            if let Some(message) = take_worker_failure() {
                return Err(io::Error::other(message).into());
            }
        }

        match message {
            ServerMessage::Evaluate { language, source } => {
                interrupt::clear();
                let result = evaluate_cell(
                    Cell { language, source },
                    &self.r,
                    &mut self.python,
                    &mut self.sql,
                );
                interrupt::clear();

                if core::is_shutting_down() {
                    return Ok(false);
                }
                if let Some(message) = take_worker_failure().or_else(|| result.err()) {
                    return Err(io::Error::other(message).into());
                }
                self.writer.send(&WorkerMessage::Completed)?;
            }
            ServerMessage::PreparePython { packages } => {
                let result = self.python.prepare(packages);
                if core::is_shutting_down() {
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
                let result = self.r.prepare(&library);
                if core::is_shutting_down() {
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

fn evaluate_cell(
    cell: Cell,
    r: &embedded_r::Runtime,
    python: &mut crate::python::Runtime,
    sql: &mut crate::sql::Bridge,
) -> Result<(), String> {
    r.idle()?;
    if core::is_shutting_down() {
        return Ok(());
    }
    if let Some(message) = take_worker_failure() {
        return Err(message);
    }
    let result = if cell.source.contains('\0') {
        let message = match cell.language {
            Language::R => "Error: R source cannot contain NUL\n",
            Language::Python => "SyntaxError: source code string cannot contain null bytes\n",
            Language::Sql => "Error: SQL source cannot contain NUL\n",
        };
        emit_output(ConsoleChannel::Diagnostic, message.as_bytes());
        Ok(())
    } else {
        r.begin_cell(cell.language)?;
        let result = match cell.language {
            Language::R => r.evaluate(cell.source),
            Language::Python => python.evaluate(&cell.source),
            Language::Sql => sql.evaluate(&cell.source),
        };
        interrupt::clear();
        r.finish_cell(cell.language)?;
        result
    };
    finish_console_stdin_operation()?;
    if result.is_ok() && !core::is_shutting_down() {
        if let Some(message) = take_worker_failure() {
            return Err(message);
        }
        r.idle()?;
    }
    result
}
