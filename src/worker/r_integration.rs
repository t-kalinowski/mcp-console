use super::{core, embedded_r, interrupt};

use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::OnceLock;

use crate::local_runtime::RInstallation;

static R_INSTALLATION: OnceLock<Option<RInstallation>> = OnceLock::new();
thread_local! {
    static RUNTIME: RefCell<Option<Rc<embedded_r::Runtime>>> = const { RefCell::new(None) };
    static STARTED: Cell<bool> = const { Cell::new(false) };
    static FAILED: Cell<bool> = const { Cell::new(false) };
}

// Clone the process-lifetime handle before interpreter calls: synchronous
// R -> Python -> R callbacks must never reenter a borrowed runtime slot.
fn runtime() -> Option<Rc<embedded_r::Runtime>> {
    RUNTIME.with(|runtime| runtime.borrow().clone())
}

pub(crate) fn initialized() -> bool {
    runtime().is_some()
}

pub(crate) fn available() -> bool {
    R_INSTALLATION.get().is_some_and(Option::is_some)
}

pub(crate) fn ensure_initialized() -> Result<(), String> {
    if FAILED.with(Cell::get) {
        return Err("R initialization is incomplete; restart required".into());
    }
    if initialized() {
        return Ok(());
    }
    let installation = R_INSTALLATION
        .get()
        .and_then(Option::as_ref)
        .ok_or("R is unavailable in this session")?;
    if STARTED.with(|started| started.replace(true)) {
        return Err("R initialization is incomplete; restart required".into());
    }
    let result = initialize(installation);
    FAILED.with(|failed| failed.set(result.is_err()));
    result
}

fn initialize(installation: &RInstallation) -> Result<(), String> {
    let deferred = embedded_r::initialize_r(installation).map_err(|error| error.to_string())?;
    crate::python::configure_r_environment().map_err(|error| error.to_string())?;
    let runtime = Rc::new(embedded_r::Runtime::initialize().map_err(|error| error.to_string())?);
    RUNTIME.with(|slot| *slot.borrow_mut() = Some(runtime.clone()));
    if core::cell_language().is_some_and(|language| !matches!(language, crate::cell::Language::Sql))
    {
        runtime.begin_graphics()?;
    }
    crate::python::attach_r_adapter()?;
    crate::python::finish_r_startup(deferred)?;
    crate::sql::attach_r()?;
    interrupt::reinstall().map_err(|error| error.to_string())?;
    crate::python::reinstall_services()?;
    Ok(())
}

pub(crate) fn ensure_bridge() -> Result<(), String> {
    ensure_initialized()?;
    if crate::python::attach_bridge()? {
        Ok(())
    } else {
        Err("R/Python attachment did not complete; retry the operation".into())
    }
}

pub(super) struct Integration;

impl Integration {
    pub(super) fn new(installation: Option<RInstallation>) -> std::io::Result<Self> {
        R_INSTALLATION
            .set(installation)
            .map_err(|_| std::io::Error::other("R capability already configured"))?;
        interrupt::initialize_native()?;
        Ok(Self)
    }

    pub(super) fn wait_for_activity(&self, sideband_fd: libc::c_int) -> Result<bool, String> {
        if initialized() {
            embedded_r::wait_for_activity(sideband_fd)
        } else {
            interrupt::wait_for_activity(sideband_fd)
        }
    }

    pub(super) fn idle(&self) -> Result<(), String> {
        if let Some(r) = runtime() {
            r.idle()
        } else {
            // Consume idle SIGINT before admitting another cell. CPython's
            // pending hook then sees acknowledged state and returns normally.
            interrupt::acknowledge_python_interrupt();
            core::observe_stdin_shutdown()
        }
    }

    pub(super) fn check_interrupts(&self) {
        if initialized() {
            embedded_r::check_interrupts();
        }
    }

    pub(super) fn begin_graphics(&self) -> Result<(), String> {
        if let Some(r) = runtime() {
            r.begin_graphics()
        } else {
            Ok(())
        }
    }

    pub(super) fn finish_graphics(&self) -> Result<(), String> {
        if let Some(r) = runtime() {
            r.finish_graphics()
        } else {
            Ok(())
        }
    }

    pub(super) fn evaluate_r(&self, source: String) -> Result<(), String> {
        ensure_initialized()?;
        runtime().unwrap().evaluate(source)
    }

    pub(super) fn prepare_r(
        &self,
        library: &str,
    ) -> Result<crate::r_environment::PreparationOutcome, String> {
        if !initialized() {
            // Library preparation must not initialize an unused interpreter.
            unsafe { std::env::set_var("R_LIBS", library) };
            return Ok(crate::r_environment::PreparationOutcome::Prepared {
                library: library.into(),
            });
        }
        runtime().unwrap().prepare(library)
    }
}
