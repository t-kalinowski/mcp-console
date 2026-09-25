use std::error::Error;
use std::io;

use super::core::{CommandReadiness, emit_output, take_worker_failure};
use super::input::finish_console_stdin_operation;
use super::r_integration::Integration;
use super::{core, embedded_r, interrupt};
use crate::cell::{Cell, Language};
use crate::worker_protocol::{ConsoleChannel, ServerMessage, WorkerMessage};

struct Coordinator {
    writer: crate::sideband::Writer,
    r: Integration,
    python: crate::python::Runtime,
    sql: Option<crate::sql::Bridge>,
}

pub(crate) fn run() -> Result<(), Box<dyn Error>> {
    let (reader, writer) = crate::sideband::connect_from_env()?;
    let selection = crate::local_runtime::Selection::from_environment()?;
    interrupt::normalize_signal()?;
    let (r, python, sql) =
        if let Some(crate::local_runtime::Selection::Python { selected, .. }) = selection {
            // Native sandbox launches supply runner-owned private storage. Direct
            // launches supply a directory retained by the server's relay lifetime.
            let temporary = std::env::var_os("TMPDIR")
                .ok_or("Python worker launch did not supply temporary storage")?;
            crate::python::configure_native_worker_environment(std::path::Path::new(&temporary))?;
            core::initialize(reader, writer.clone())?;
            let r = Integration::new(None)?;
            let python = crate::python::Runtime::native(&selected)?;
            (r, python, None)
        } else {
            let r_home = harp::command::r_home_setup()?;
            #[cfg(target_os = "linux")]
            reexec_with_r_library_path(&r_home, &reader, &writer)?;
            let temporary_directory = embedded_r::initialize_r(&r_home)?;
            crate::python::configure_worker_environment(&temporary_directory)?;
            core::initialize(reader, writer.clone())?;
            let r = Integration::new(Some(embedded_r::Runtime::initialize()?))?;
            let python = crate::python::Runtime::initialize()?;
            let sql = Some(crate::sql::Bridge::initialize()?);
            (r, python, sql)
        };
    writer.send(&WorkerMessage::Ready)?;
    let mut coordinator = Coordinator {
        writer,
        r,
        python,
        sql,
    };
    let result = coordinator.run();
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

impl Coordinator {
    fn run(&mut self) -> Result<(), Box<dyn Error>> {
        loop {
            if !self.handle(Self::wait_for_message(&self.r)?)? {
                return Ok(());
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
                self.r.check_interrupts();
                let result = evaluate_cell(
                    Cell { language, source },
                    &self.r,
                    &mut self.python,
                    &mut self.sql,
                );
                self.r.check_interrupts();

                if core::is_shutting_down() {
                    return Ok(false);
                }
                if let Some(message) = take_worker_failure().or_else(|| result.err()) {
                    return Err(io::Error::other(message).into());
                }
                self.writer.send(&WorkerMessage::Completed)?;
            }
            // Keep worker-owned preparation state transitions atomic. Any
            // nested host resolver registers its own interrupt target.
            ServerMessage::PreparePython { packages } => {
                let result = self.r.prepare_python(|| self.python.prepare(packages));
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
                let result = self.r.prepare_r(&library);
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

    fn wait_for_message(r: &Integration) -> Result<ServerMessage, String> {
        loop {
            let sideband_fd = match core::next_command()? {
                CommandReadiness::Ready(message) => return Ok(message),
                CommandReadiness::Waiting(descriptor) => descriptor,
            };
            // R activity must service callbacks before the next wait. The
            // native wait also wakes for interrupts and input shutdown.
            if r.wait_for_activity(sideband_fd)? {
                return core::receive_server_message();
            }

            r.idle()?;
            if let Some(message) = take_worker_failure() {
                return Err(message);
            }
        }
    }
}

fn evaluate_cell(
    cell: Cell,
    r: &Integration,
    python: &mut crate::python::Runtime,
    sql: &mut Option<crate::sql::Bridge>,
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
        // Python can enter R and create plots too. SQL retains its exclusion.
        let graphics = !matches!(cell.language, Language::Sql);
        if graphics {
            r.begin_graphics()?;
        }
        core::begin_cell(cell.language);
        let result = match cell.language {
            Language::R => r.evaluate_r(cell.source),
            Language::Python => python.evaluate(&cell.source),
            Language::Sql => sql
                .as_mut()
                .expect("SQL admission requires R")
                .evaluate(&cell.source),
        };
        core::finish_cell();
        if graphics {
            r.finish_graphics()?;
        }
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

#[cfg(test)]
#[path = "../../tests/fixtures/native_worker.rs"]
mod tests;
