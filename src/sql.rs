mod dbapi;
mod dbi;

/// Worker-facing SQL runtime router.
///
/// R DBI connections stay in embedded R, while Python DB-API connections stay
/// in CPython. Rust chooses the active provider for each SQL cell without
/// converting connection objects or result rows between the runtimes.
pub(crate) struct Bridge {
    dbi: dbi::Backend,
}

impl Bridge {
    pub(crate) fn initialize() -> Result<Self, String> {
        Ok(Self {
            dbi: dbi::Backend::initialize()?,
        })
    }

    pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        match dbapi::dispatch(source)? {
            dbapi::Provider::Python => Ok(()),
            dbapi::Provider::Managed => {
                self.dbi.restore_managed()?;
                self.dbi.evaluate(source)
            }
            dbapi::Provider::R => self.dbi.evaluate(source),
        }
    }
}

pub(crate) fn install_python_runtime() -> Result<(), String> {
    dbapi::install_runtime()
}
