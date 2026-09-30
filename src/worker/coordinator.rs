use std::error::Error;
use std::io;

use super::core::{CommandReadiness, emit_output, take_worker_failure};
use super::input::finish_console_stdin_operation;
use super::r_integration::Integration;
use super::{core, interrupt};
use crate::cell::{Cell, Language};
use crate::worker_protocol::{ConsoleChannel, ServerMessage, WorkerMessage};

struct Coordinator {
    initialized: bool,
    writer: crate::sideband::Writer,
    r: Integration,
    python: crate::python::Runtime,
    sql: crate::sql::Bridge,
}

pub(crate) fn run() -> Result<(), Box<dyn Error>> {
    let result = run_session();
    // Every return, including startup and readiness failures, must restore
    // Python's initial thread before extension-library process destructors.
    crate::python::prepare_process_exit()?;
    result
}

fn run_session() -> Result<(), Box<dyn Error>> {
    let (reader, writer) = crate::sideband::connect_from_env()?;
    let selection = crate::local_runtime::Selection::from_environment()?
        .ok_or("worker launch did not supply a complete runtime selection")?;
    interrupt::normalize_signal()?;
    let r_home = selection.r.then(crate::local_runtime::r_home).transpose()?;
    #[cfg(target_os = "linux")]
    if let Some(home) = &r_home {
        reexec_with_r_library_path(home, &reader, &writer)?;
    }
    // The launcher owns this directory through confirmed worker retirement.
    // R's session tempdir is a child, never the owner of Python/SQL storage.
    let temporary =
        std::env::var_os("TMPDIR").ok_or("worker launch did not supply temporary storage")?;
    crate::python::configure_native_worker_environment(std::path::Path::new(&temporary))?;
    core::initialize(reader, writer.clone())?;
    let r = Integration::new(r_home)?;
    let python = crate::python::Runtime::new(selection)?;
    let sql = crate::sql::Bridge::new();
    writer.send(&WorkerMessage::Ready)?;
    let mut coordinator = Coordinator {
        initialized: false,
        writer,
        r,
        python,
        sql,
    };
    coordinator.run()
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
            ServerMessage::Initialize => {
                if self.initialized {
                    return Err(io::Error::other("runtime initialization already completed").into());
                }
                // Startup owns graphics and managed input without a user cell.
                // R opens this scope before loading its default packages.
                let result = (|| {
                    if crate::worker::r_available() {
                        super::r_integration::ensure_initialized()?;
                    }
                    self.python.initialize()?;
                    if crate::worker::r_available() && crate::python::bridge_available()? {
                        super::r_integration::ensure_bridge()?;
                    }
                    self.sql.initialize()?;
                    Ok::<(), String>(())
                })();
                self.r.finish_graphics()?;
                finish_console_stdin_operation()?;
                if core::is_shutting_down() {
                    return Ok(false);
                }
                result.map_err(io::Error::other)?;
                if let Some(message) = take_worker_failure() {
                    return Err(io::Error::other(message).into());
                }
                self.initialized = true;
                self.writer.send(&WorkerMessage::Initialized)?;
            }
            ServerMessage::Evaluate { language, source } => {
                if !self.initialized {
                    return Err(io::Error::other("runtime initialization has not completed").into());
                }
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
            // Resolution, inspection, and site hooks remain interruptible;
            // the native owner defers interrupts only around publication.
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
                    Ok(crate::python::PreparationOutcome::Rejected { message }) => {
                        self.writer
                            .send(&WorkerMessage::PythonPreparationRejected { message })?;
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
    sql: &mut crate::sql::Bridge,
) -> Result<(), String> {
    r.idle()?;
    if core::is_shutting_down() {
        return Ok(());
    }
    if let Some(message) = take_worker_failure() {
        return Err(message);
    }
    let nul_error = match cell.language {
        Language::R if cell.source.contains('\0') => Some("Error: R source cannot contain NUL\n"),
        Language::Sql if cell.source.contains('\0') => {
            Some("Error: SQL source cannot contain NUL\n")
        }
        _ => None,
    };
    let result = if let Some(message) = nul_error {
        emit_output(ConsoleChannel::Diagnostic, message.as_bytes());
        Ok(())
    } else {
        core::begin_cell(cell.language);
        // Python can enter R and create plots too. SQL retains its exclusion.
        let graphics = !matches!(cell.language, Language::Sql);
        if graphics {
            r.begin_graphics()?;
        }
        let result = match cell.language {
            Language::R => r.evaluate_r(cell.source),
            Language::Python => python.evaluate(&cell.source),
            Language::Sql => sql.evaluate(&cell.source),
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
