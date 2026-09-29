mod py_dbapi;
mod r_dbi;

thread_local! {
    static R_BACKEND: std::cell::OnceCell<std::rc::Rc<r_dbi::Backend>> = const { std::cell::OnceCell::new() };
}

pub(crate) fn attach_r() -> Result<(), String> {
    let backend = std::rc::Rc::new(r_dbi::Backend::initialize()?);
    R_BACKEND
        .with(|slot| slot.set(backend))
        .map_err(|_| "R SQL backend already attached".to_string())
}

/// Worker-facing SQL runtime router.
///
/// R DBI connections stay in embedded R, while Python DB-API connections stay
/// in CPython. Rust chooses the active provider for each SQL cell without
/// converting connection objects or result rows between the runtimes.
pub(crate) struct Bridge;

impl Bridge {
    pub(crate) fn initialize(&self) -> Result<(), String> {
        if crate::worker::r_available() {
            self.r_backend()?.initialize_managed()
        } else {
            crate::python::initialize_managed_sql()
        }
    }

    pub(crate) fn new() -> Self {
        Self
    }

    fn r_backend(&self) -> Result<std::rc::Rc<r_dbi::Backend>, String> {
        R_BACKEND
            .with(|slot| slot.get().cloned())
            .ok_or("R SQL backend is unavailable".into())
    }

    pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        match py_dbapi::dispatch(source)? {
            py_dbapi::Provider::Handled => Ok(()),
            py_dbapi::Provider::Managed => {
                let r_dbi = self.r_backend()?;
                r_dbi.restore_managed()?;
                r_dbi.evaluate(source)
            }
            py_dbapi::Provider::R => self.r_backend()?.evaluate(source),
        }
    }
}

pub(crate) fn install_python_runtime() -> Result<(), String> {
    py_dbapi::install_runtime()
}
