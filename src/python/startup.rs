use super::{ImportResolution, PreparationOutcome, reticulate};
use std::path::Path;

#[derive(Debug, serde::Deserialize)]
pub(crate) struct SelectedPython {
    pub(crate) python: String,
    pub(crate) libpython: String,
    pub(crate) python_home: String,
}

pub(crate) fn initialize_selected(selected: &SelectedPython) -> Result<bool, String> {
    super::library::initialize(
        Path::new(&selected.libpython),
        &selected.python,
        &selected.python_home,
    )
}

pub(super) fn install_services(libpython: &Path) -> Result<(), String> {
    // Reticulate calls this after its own stream and input hooks. Reasserting
    // the interrupt handler is intentional when those hooks run again.
    super::library::load(libpython)?;
    super::library::install_services()
}

/// Install the shared evaluator after the selected interpreter is live.
/// Reticulate may call this from its initialization hook, while a native
/// caller can pass no callback without constructing an R adapter.
/// Successful steps remain in the process-lifetime library state, so a later
/// call resumes incomplete setup without replacing the interpreter.
pub(crate) fn setup_runtime(
    libpython: &Path,
    resolution: ImportResolution<'_>,
) -> Result<bool, String> {
    super::library::load(libpython)?;
    if super::library::runtime_configured()? {
        return Ok(true);
    }
    // The hook may already have installed services. Native callers arrive
    // here directly, and perform the same installation once.
    if !super::library::services_installed()? {
        install_services(libpython)?;
    }
    super::library::install_runtime(super::RUNTIME_SOURCE)?;
    crate::sql::install_python_runtime()?;
    // The finder starts with automatic resolution disabled. A native caller
    // with neither input keeps that default rather than replacing its reason
    // with Python None.
    if (resolution.callback.is_some() || resolution.disabled_reason.is_some())
        && !super::library::configure_import_resolution(resolution)?
    {
        return Ok(false);
    }
    super::library::mark_runtime_configured()?;
    Ok(true)
}

pub(crate) fn finish_initialization() -> Result<(), String> {
    super::library::finish_initialization()
}

/// Native owner for both Python-first and R-first interpreter startup.
pub(super) struct Runtime {
    adapter: reticulate::Adapter,
    completed: bool,
}

impl Runtime {
    pub(super) fn initialize() -> Result<Self, String> {
        Ok(Self {
            adapter: reticulate::Adapter::initialize()?,
            completed: false,
        })
    }

    pub(super) fn ensure_initialized(&mut self) -> Result<bool, String> {
        if !self.completed {
            let Some(selected) = self.adapter.select()? else {
                return Ok(false);
            };
            if let Err(error) = initialize_selected(&selected) {
                self.adapter.cancel_selection()?;
                return Err(error);
            }
            // Reticulate attaches conversion and event integration first.
            // Its initialization hook enters the shared native setup above;
            // the explicit setup call also covers an already-live interpreter.
            let result = self.adapter.attach().and_then(|attached| {
                if attached {
                    self.adapter.setup()
                } else {
                    Ok(false)
                }
            });
            let finished = finish_initialization();
            let completed = result?;
            finished?;
            self.completed = completed;
        }
        Ok(self.completed)
    }

    pub(super) fn prepare(&self, packages: Vec<String>) -> Result<PreparationOutcome, String> {
        self.adapter.prepare(packages)
    }
}
