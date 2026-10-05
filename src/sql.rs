mod py_dbapi;
mod r_dbi;

static SETTINGS: std::sync::OnceLock<crate::settings::sql::Sql> = std::sync::OnceLock::new();

pub(crate) fn configure() -> Result<(), String> {
    SETTINGS
        .set(crate::settings::sql::Sql::from_environment()?)
        .map_err(|_| "SQL settings already captured".into())
}

pub(crate) fn managed_is_r() -> bool {
    match SETTINGS
        .get()
        .expect("worker SQL settings captured")
        .provider
    {
        crate::settings::sql::Provider::Auto => crate::worker::r_available(),
        crate::settings::sql::Provider::R => true,
        crate::settings::sql::Provider::Python => false,
    }
}

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
        if managed_is_r() {
            if !crate::worker::r_available() {
                return Ok(());
            }
            self.r_backend()?.initialize_managed()
        } else {
            crate::python::initialize_managed_sql()
        }
    }

    pub(crate) fn new() -> Self {
        Self
    }

    fn r_backend(&self) -> Result<std::rc::Rc<r_dbi::Backend>, String> {
        crate::worker::ensure_r()?;
        R_BACKEND
            .with(|slot| slot.get().cloned())
            .ok_or("R SQL backend is unavailable".into())
    }

    pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        if !managed_is_r() && !crate::python::ensure_initialized()? {
            return Ok(());
        }
        match py_dbapi::dispatch(source)? {
            py_dbapi::Provider::Handled => Ok(()),
            provider @ (py_dbapi::Provider::Managed | py_dbapi::Provider::R) => {
                if !crate::worker::r_available() {
                    crate::worker::emit_output(
                        crate::worker_protocol::ConsoleChannel::Diagnostic,
                        b"Error: R is unavailable in this session\n",
                    );
                    return Ok(());
                }
                let r_dbi = self.r_backend()?;
                if matches!(provider, py_dbapi::Provider::Managed) {
                    r_dbi.restore_managed()?;
                }
                r_dbi.evaluate(source)
            }
        }
    }
}

pub(crate) fn install_python_runtime() -> Result<bool, String> {
    py_dbapi::install_runtime()
}
