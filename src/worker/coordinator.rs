use std::error::Error;
use std::io;

#[cfg(all(test, unix))]
use super::activity::CommandReadiness;
use super::core::{emit_output, take_worker_failure};
use super::input::finish_console_stdin_operation;
use super::r_integration::Integration;
use super::{activity, bootstrap, core, interrupt};
use crate::cell::{Cell, Language};
use crate::worker_protocol::{ConsoleChannel, ServerMessage, WorkerMessage};

struct Coordinator {
    writer: crate::sideband::Writer,
    r: Integration,
    python: crate::python::Runtime,
    sql: crate::sql::Bridge,
}

pub(crate) fn run(bootstrap_runtimes: bool) -> Result<(), Box<dyn Error>> {
    let result = run_session(bootstrap_runtimes);
    // Every return, including startup and readiness failures, must restore
    // Python's initial thread before extension-library process destructors.
    crate::python::prepare_process_exit()?;
    result
}

fn run_session(bootstrap_runtimes: bool) -> Result<(), Box<dyn Error>> {
    bootstrap::configure_stdio()?;
    let (reader, writer) = crate::sideband::connect_from_env()?;
    let selection = crate::local_runtime::Selection::from_environment()?.unwrap_or(
        crate::local_runtime::WorkerSelection {
            r: true,
            python: None,
        },
    );
    interrupt::normalize_signal()?;
    let r_installation = selection
        .r
        .then(crate::local_runtime::r_installation)
        .transpose()?;
    bootstrap::prepare_r_library_path(r_installation.as_ref(), &reader, &writer)?;
    // The launcher owns this directory through confirmed worker retirement.
    // R's session tempdir is a child, never the owner of Python/SQL storage.
    let temporary =
        std::env::var_os("TMPDIR").ok_or("worker launch did not supply temporary storage")?;
    crate::python::configure_native_worker_environment(std::path::Path::new(&temporary))?;
    core::initialize(reader, writer.clone())?;
    let r = Integration::new(r_installation)?;
    let python = crate::python::Runtime::new(selection)?;
    crate::sql::configure()?;
    let sql = crate::sql::Bridge::new();
    writer.send(&WorkerMessage::Ready)?;
    let mut coordinator = Coordinator {
        writer,
        r,
        python,
        sql,
    };
    coordinator.run(bootstrap_runtimes)
}

impl Coordinator {
    fn wait_for_message(r: &Integration) -> Result<ServerMessage, String> {
        activity::wait_for_message(r)
    }

    fn run(&mut self, bootstrap_runtimes: bool) -> Result<(), Box<dyn Error>> {
        if bootstrap_runtimes {
            self.initialize_runtimes()?;
        }
        while !core::is_shutting_down() {
            if !self.handle(Self::wait_for_message(&self.r)?)? {
                break;
            }
        }
        Ok(())
    }

    fn initialize_runtimes(&mut self) -> Result<(), Box<dyn Error>> {
        // Ready connects callbacks before hooks run. Bootstrap uses the same
        // serialized interpreter thread and never enters a user evaluation.
        let languages = crate::cell::Languages::from_environment()?;
        core::set_bootstrapping(true);
        let complete = if languages.enables(Language::Python) {
            crate::python::ensure_initialized().map_err(io::Error::other)?
        } else {
            true
        };
        if complete && languages.enables(Language::R) && super::r_available() {
            super::ensure_r().map_err(io::Error::other)?;
        }
        if complete && languages.enables(Language::Sql) && !core::bootstrap_interrupted() {
            self.sql.initialize().map_err(io::Error::other)?;
        }
        self.r.finish_graphics().map_err(io::Error::other)?;
        // Acknowledge late signals before publishing the bootstrap receipt.
        let interrupted =
            interrupt::acknowledge_python_interrupt() || core::bootstrap_interrupted();
        core::set_bootstrapping(false);
        finish_console_stdin_operation()?;
        if core::is_shutting_down() {
            return Ok(());
        }
        if let Some(message) = take_worker_failure() {
            return Err(io::Error::other(message).into());
        }
        self.writer
            .send(&WorkerMessage::RuntimeInitialized { interrupted })?;
        Ok(())
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
        // Runtime startup belongs to this cell too. A late R startup begins
        // graphics when it installs its runtime, before loading packages.
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

#[cfg(all(test, unix))]
#[path = "../../tests/fixtures/native_worker.rs"]
mod tests;
