use super::{core, embedded_r, interrupt};

use std::cell::{Cell, RefCell};
use std::path::PathBuf;
use std::rc::Rc;
use std::sync::OnceLock;

static R_HOME: OnceLock<Option<PathBuf>> = OnceLock::new();
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
    R_HOME.get().is_some_and(Option::is_some)
}

pub(crate) fn ensure_initialized() -> Result<(), String> {
    if FAILED.with(Cell::get) {
        return Err("R initialization is incomplete; restart required".into());
    }
    if initialized() {
        return Ok(());
    }
    let home = R_HOME
        .get()
        .and_then(Option::as_ref)
        .ok_or("R is unavailable in this session")?;
    if STARTED.with(|started| started.replace(true)) {
        return Err("R initialization is incomplete; restart required".into());
    }
    let result = initialize(home);
    FAILED.with(|failed| failed.set(result.is_err()));
    result
}

fn initialize(home: &std::path::Path) -> Result<(), String> {
    let deferred = embedded_r::initialize_r(home).map_err(|error| error.to_string())?;
    crate::python::configure_r_environment().map_err(|error| error.to_string())?;
    let runtime = Rc::new(embedded_r::Runtime::initialize().map_err(|error| error.to_string())?);
    RUNTIME.with(|slot| *slot.borrow_mut() = Some(runtime.clone()));
    runtime.begin_graphics()?;
    crate::python::attach_r_adapter()?;
    crate::python::finish_r_startup(deferred)?;
    crate::sql::attach_r()?;
    interrupt::reinstall().map_err(|error| error.to_string())?;
    crate::python::reinstall_services()?;
    Ok(())
}

pub(crate) fn ensure_bridge() -> Result<(), String> {
    if !initialized() {
        return Err("R is unavailable in this session".into());
    }
    if crate::python::attach_bridge()? {
        Ok(())
    } else {
        Err("R/Python attachment did not complete; restart required".into())
    }
}

pub(super) struct Integration;

impl Integration {
    pub(super) fn new(home: Option<PathBuf>) -> std::io::Result<Self> {
        R_HOME
            .set(home)
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

    pub(super) fn prepare_python<T>(
        &self,
        operation: impl FnOnce() -> Result<T, String>,
    ) -> Result<T, String> {
        if initialized() {
            embedded_r::defer_interrupts(operation, embedded_r::discard_interrupts)
        } else {
            let result = operation();
            interrupt::acknowledge_python_interrupt();
            result
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
        runtime()
            .ok_or("R is unavailable in this session")?
            .evaluate(source)
    }

    pub(super) fn prepare_r(
        &self,
        library: &str,
    ) -> Result<crate::r_environment::PreparationOutcome, String> {
        runtime()
            .ok_or("R is unavailable in this session")?
            .prepare(library)
    }
}
