mod py_dbapi;
mod r_dbi;

use std::cell::{OnceCell, RefCell};
use std::sync::{
    OnceLock,
    atomic::{AtomicBool, Ordering},
};

static MANAGED_R: OnceLock<bool> = OnceLock::new();
static EVALUATING: AtomicBool = AtomicBool::new(false);
thread_local! {
    static R_BACKEND: OnceCell<RefCell<r_dbi::Backend>> = const { OnceCell::new() };
}

/// SQL owns provider selection. Its adapters retain their native connection
/// objects; activating another language never changes the managed provider.
pub(crate) struct Bridge;

impl Bridge {
    pub(crate) fn initialize(managed_r: bool) -> Result<Self, String> {
        MANAGED_R
            .set(managed_r)
            .map_err(|_| "SQL provider already selected")?;
        Ok(Self)
    }

    pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        if !managed_r()
            && let Err(message) = crate::python::ensure_initialized()
        {
            crate::worker::emit_diagnostic(&format!("Error: {message}\n"));
            return Ok(());
        }
        EVALUATING.store(true, Ordering::SeqCst);
        let result = (|| match py_dbapi::dispatch(source)? {
            py_dbapi::Provider::Handled => Ok(()),
            provider => {
                crate::worker::activate_r()?;
                R_BACKEND.with(|backend| {
                    let mut backend = backend.get().expect("active R SQL adapter").borrow_mut();
                    if matches!(provider, py_dbapi::Provider::Managed) {
                        backend.restore_managed()?;
                    }
                    backend.evaluate(source)
                })
            }
        })();
        EVALUATING.store(false, Ordering::SeqCst);
        result
    }
}

pub(crate) fn managed_r() -> bool {
    *MANAGED_R.get().expect("SQL provider selected")
}
pub(crate) fn evaluating() -> bool {
    EVALUATING.load(Ordering::SeqCst)
}

pub(crate) fn activate_r_adapter() -> Result<(), String> {
    R_BACKEND.with(|backend| {
        if backend.get().is_none() {
            backend
                .set(RefCell::new(r_dbi::Backend::initialize()?))
                .map_err(|_| "R SQL adapter already initialized")?;
        }
        Ok(())
    })
}

pub(crate) fn install_python_runtime() -> Result<(), String> {
    py_dbapi::install_runtime()
}
