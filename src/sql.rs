mod py_dbapi;
mod r_dbi;

/// Worker-facing SQL runtime router.
///
/// R DBI connections stay in embedded R, while Python DB-API connections stay
/// in CPython. Rust chooses the active provider for each SQL cell without
/// converting connection objects or result rows between the runtimes.
pub(crate) struct Bridge {
    r_dbi: r_dbi::Backend,
}

impl Bridge {
    pub(crate) fn initialize() -> Result<Self, String> {
        Ok(Self {
            r_dbi: r_dbi::Backend::initialize()?,
        })
    }

    pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        match py_dbapi::dispatch(source)? {
            py_dbapi::Provider::Handled => Ok(()),
            py_dbapi::Provider::Managed => {
                self.r_dbi.restore_managed()?;
                self.r_dbi.evaluate(source)
            }
            py_dbapi::Provider::R => self.r_dbi.evaluate(source),
        }
    }
}

pub(crate) fn install_python_runtime() -> Result<(), String> {
    py_dbapi::install_runtime()
}
