use libr::SEXP;

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
            let selected = self.adapter.select()?;
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

// Rust initializes the exact interpreter selected by reticulate. Reticulate
// then observes the running interpreter and attaches its conversion runtime.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_initialize_python(
    python: SEXP,
    libpython: SEXP,
    python_home: SEXP,
) -> harp::Result<SEXP> {
    let python = String::try_from(harp::object::RObject::view(python))?;
    let libpython = Option::<String>::try_from(harp::object::RObject::view(libpython))?
        .ok_or_else(|| harp::anyhow!("Python-hosted R is not supported"))?;
    let python_home = String::try_from(harp::object::RObject::view(python_home))?;
    let rust_owned =
        super::library::initialize(std::path::Path::new(&libpython), &python, &python_home)
            .map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(rust_owned).sexp)
}

// If Python was initialized before the direct initializer was installed,
// attach the Rust-owned process-lifetime handle to that interpreter.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_load_python_library(path: SEXP) -> harp::Result<SEXP> {
    let path = Option::<String>::try_from(harp::object::RObject::view(path))?
        .ok_or_else(|| harp::anyhow!("Python-hosted R is not supported"))?;
    let rust_owned = super::library::load(std::path::Path::new(&path))
        .map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(rust_owned).sexp)
}

// Reticulate's initialization lifecycle installs Console's native services.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_install_python_services(
    libpython: SEXP,
) -> harp::Result<SEXP> {
    let libpython = String::try_from(harp::object::RObject::view(libpython))?;
    super::library::load(std::path::Path::new(&libpython))
        .and_then(|_| super::library::install_services())
        .map_err(|error| harp::anyhow!("{error}"))?;
    unsafe { Ok(libr::R_NilValue) }
}

// Install the private evaluator through the Rust-owned CPython API while
// retaining reticulate's existing post-initialization lifecycle point.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_install_python_runtime(libpython: SEXP) -> harp::Result<SEXP> {
    let libpython = Option::<String>::try_from(harp::object::RObject::view(libpython))?
        .ok_or_else(|| harp::anyhow!("Python-hosted R is not supported"))?;
    super::library::load(std::path::Path::new(&libpython))
        .map_err(|error| harp::anyhow!("{error}"))?;
    super::library::install_runtime(super::RUNTIME_SOURCE)
        .map_err(|error| harp::anyhow!("{error}"))?;
    crate::sql::install_python_runtime().map_err(|error| harp::anyhow!("{error}"))?;
    unsafe { Ok(libr::R_NilValue) }
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_runtime_configured() -> harp::Result<SEXP> {
    super::library::mark_runtime_configured().map_err(|error| harp::anyhow!("{error}"))?;
    unsafe { Ok(libr::R_NilValue) }
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_runtime_is_configured() -> harp::Result<SEXP> {
    let configured =
        super::library::runtime_configured().map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(configured).sexp)
}

// Release the initial GIL when control leaves reticulate's C initializer,
// including its error paths. Later reticulate calls acquire the GIL normally.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_finish_python_initialization() -> harp::Result<SEXP> {
    super::library::finish_initialization().map_err(|error| harp::anyhow!("{error}"))?;
    unsafe { Ok(libr::R_NilValue) }
}

// Register this callback explicitly: harp::register suspends interrupts
// throughout the call. Leave Python setup in the caller's interrupt context.
#[ctor::ctor(unsafe)]
fn register_python_setup() {
    type CallMethod = unsafe extern "C-unwind" fn() -> *mut libc::c_void;
    // SAFETY: R invokes each fixed callback with its registered arity on the
    // worker's R thread. The names have static storage.
    unsafe {
        harp::routines::add(libr::R_CallMethodDef {
            name: c"mcp_console_disable_matplotlib_show".as_ptr(),
            fun: Some(std::mem::transmute::<*const (), CallMethod>(
                mcp_console_disable_matplotlib_show as *const (),
            )),
            numArgs: 0,
        });
    }
}

#[allow(clippy::result_large_err)]
extern "C-unwind" fn mcp_console_disable_matplotlib_show() -> SEXP {
    harp::exec::r_unwrap(|| -> harp::Result<SEXP> {
        let completed =
            super::library::disable_matplotlib_show().map_err(|error| harp::anyhow!("{error}"))?;
        harp::exec::r_sandbox(|| harp::object::RObject::from(completed).sexp)
    })
}
