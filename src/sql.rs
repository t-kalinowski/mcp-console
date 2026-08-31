mod dbapi;
mod dbi;

pub(crate) enum Provider {
    R,
    Managed,
    Python,
}

/// Worker-facing SQL runtime router.
///
/// R DBI connections stay in embedded R, while Python DB-API connections stay
/// in CPython. Rust chooses the active provider for each SQL cell without
/// converting connection objects or result rows between the runtimes.
pub(crate) struct Bridge {
    dbi: dbi::Backend,
    dbapi: dbapi::Backend,
}

impl Bridge {
    pub(crate) fn initialize() -> Result<Self, String> {
        Ok(Self {
            dbi: dbi::Backend::initialize()?,
            dbapi: dbapi::Backend::initialize(),
        })
    }

    pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        match self.dbapi.provider()? {
            Provider::Python => self.dbapi.evaluate(source),
            Provider::Managed => {
                self.dbi.restore_managed()?;
                self.dbi.evaluate(source)
            }
            Provider::R => self.dbi.evaluate(source),
        }
    }
}
