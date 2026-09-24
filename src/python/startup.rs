use super::{PreparationOutcome, reticulate};

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
            if let Err(error) = super::library::initialize(
                std::path::Path::new(&selected.libpython),
                &selected.python,
                &selected.python_home,
            ) {
                self.adapter.cancel_selection()?;
                return Err(error);
            }
            // Reticulate attaches after CPython is live. Its R boundary still
            // converts setup errors and interruptions for the worker.
            let result = self.adapter.attach_and_setup();
            let finished = super::library::finish_initialization();
            self.completed = result?;
            finished?;
        }
        Ok(self.completed)
    }

    pub(super) fn prepare(&self, packages: Vec<String>) -> Result<PreparationOutcome, String> {
        self.adapter.prepare(packages)
    }
}
