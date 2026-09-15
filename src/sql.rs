mod py_dbapi;
mod r_dbi;

/// Worker-facing SQL runtime router.
///
/// R DBI connections stay in embedded R, while Python DB-API connections stay
/// in CPython. Rust chooses the active provider for each SQL cell without
/// converting connection objects or result rows between the runtimes.
pub(crate) struct Bridge {
    r_dbi: Option<r_dbi::Backend>,
}

impl Bridge {
    pub(crate) fn initialize(managed_r: bool) -> Result<Self, String> {
        Ok(Self {
            r_dbi: managed_r.then(r_dbi::Backend::initialize).transpose()?,
        })
    }

    pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        if self.r_dbi.is_none()
            && let Err(message) = crate::python::ensure_initialized()
        {
            crate::worker::emit_diagnostic(&format!("Error: {message}\n"));
            return Ok(());
        }
        match py_dbapi::dispatch(source)? {
            py_dbapi::Provider::Handled => Ok(()),
            py_dbapi::Provider::Managed => {
                let backend = self.r_dbi.as_mut().expect("managed R SQL provider");
                backend.restore_managed()?;
                backend.evaluate(source)
            }
            py_dbapi::Provider::R => self
                .r_dbi
                .as_mut()
                .ok_or("R SQL provider is unavailable")?
                .evaluate(source),
        }
    }
}

pub(crate) fn install_python_runtime() -> Result<(), String> {
    py_dbapi::install_runtime()
}
