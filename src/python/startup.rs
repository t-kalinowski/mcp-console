use super::{ImportResolution, PreparationOutcome, reticulate};
use std::path::Path;

#[derive(Debug, serde::Deserialize, serde::Serialize)]
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
    setup_runtime_with_sql(libpython, resolution, true)
}

fn setup_runtime_with_sql(
    libpython: &Path,
    resolution: ImportResolution<'_>,
    sql: bool,
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
    if sql {
        crate::sql::install_python_runtime()?;
    }
    // The finder starts with automatic resolution disabled. A native caller
    // with neither input keeps that default rather than replacing its reason
    // with Python None.
    if (resolution.callback.is_some() || resolution.disabled_reason.is_some())
        && !super::library::configure_import_resolution(resolution)?
    {
        return Ok(false);
    }
    super::library::mark_runtime_configured(sql)?;
    Ok(true)
}

pub(crate) fn finish_initialization() -> Result<(), String> {
    super::library::finish_initialization()
}

/// Native owner for both Python-first and R-first interpreter startup.
pub(super) struct Runtime {
    adapter: Option<reticulate::Adapter>,
    completed: bool,
}

impl Runtime {
    pub(super) fn initialize() -> Result<Self, String> {
        Ok(Self {
            adapter: Some(reticulate::Adapter::initialize()?),
            completed: false,
        })
    }

    pub(super) fn native(configuration: &super::NativePython) -> Result<Self, String> {
        let selected = &configuration.embedding;
        // Let CPython's program-name/pyvenv.cfg path initialization select the
        // virtualenv. Setting PythonHome to its base overrides that selection.
        super::library::initialize(Path::new(&selected.libpython), &selected.python, "")?;
        let result = setup_runtime_with_sql(
            Path::new(&selected.libpython),
            ImportResolution {
                callback: None,
                disabled_reason: Some(crate::local_runtime::IMPORT_DISABLED),
            },
            false,
        )
        .and_then(|configured| {
            if !configured {
                return Err("native Python setup did not complete".into());
            }
            super::library::configure_native_environment(configuration)?;
            if !super::library::disable_matplotlib_show()? {
                return Err("Python plotting setup did not complete".into());
            }
            Ok(())
        });
        if result.is_err() {
            super::library::display_setup_exception()?;
        }
        let finished = finish_initialization();
        result?;
        finished?;
        Ok(Self {
            adapter: None,
            completed: true,
        })
    }

    pub(super) fn ensure_initialized(&mut self) -> Result<bool, String> {
        if !self.completed {
            let adapter = self
                .adapter
                .as_mut()
                .expect("incomplete reticulate startup");
            let Some(selected) = adapter.select()? else {
                return Ok(false);
            };
            if let Err(error) = initialize_selected(&selected) {
                adapter.cancel_selection()?;
                return Err(error);
            }
            // Reticulate attaches conversion and event integration first.
            // Its initialization hook enters the shared native setup above;
            // the explicit setup call also covers an already-live interpreter.
            let result =
                adapter.attach().and_then(
                    |attached| {
                        if attached { adapter.setup() } else { Ok(false) }
                    },
                );
            let finished = finish_initialization();
            let completed = result?;
            finished?;
            self.completed = completed;
        }
        Ok(self.completed)
    }

    pub(super) fn prepare(&self, packages: Vec<String>) -> Result<PreparationOutcome, String> {
        self.adapter
            .as_ref()
            .expect("live preparation requires R")
            .prepare(packages)
    }
}
