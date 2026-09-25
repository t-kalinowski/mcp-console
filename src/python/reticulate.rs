use libr::SEXP;

use super::{PreparationOutcome, SelectedPython};

const PYTHON_BRIDGE_SOURCE: &str = include_str!("bridge.R");
const PYTHON_INITIALIZER_SOURCE: &str = include_str!("initialize.R");

/// Reticulate selects Python, retains Console's embedding configuration, and attaches.
pub(super) struct Adapter {
    bridge: crate::r_bridge::Bridge,
}

pub(super) fn configure_worker_environment() -> std::io::Result<()> {
    super::platform::set_environment(c"RETICULATE_REMAP_OUTPUT_STREAMS", c"0", true)
}

impl Adapter {
    pub(super) fn initialize() -> Result<Self, String> {
        let source = format!(
            "base::local(\n  {{\n    state <- ({PYTHON_BRIDGE_SOURCE})\n{PYTHON_INITIALIZER_SOURCE}\n    state\n  }},\n  envir = base::new.env(parent = base::baseenv())\n)"
        );
        Ok(Self {
            bridge: crate::r_bridge::Bridge::initialize(&source, "Python")?,
        })
    }

    pub(super) fn select(&mut self) -> Result<Option<SelectedPython>, String> {
        // Discovery and serialization share the existing R interrupt boundary.
        self.bridge
            .evaluate_completed_string("select")?
            .filter(|selected| !selected.is_empty())
            .map(|selected| {
                serde_json::from_str(&selected)
                    .map_err(|error| format!("invalid selected Python configuration: {error}"))
            })
            .transpose()
    }

    pub(super) fn cancel_selection(&self) -> Result<(), String> {
        self.bridge.call0_integer(c"cancel_python_selection")?;
        Ok(())
    }

    pub(super) fn attach(&mut self) -> Result<bool, String> {
        self.bridge.evaluate_completed("attach")
    }

    pub(super) fn setup(&mut self) -> Result<bool, String> {
        self.bridge.evaluate_completed("setup")
    }

    pub(super) fn prepare(&self, packages: Vec<String>) -> Result<PreparationOutcome, String> {
        let request = serde_json::to_string(&packages)
            .map_err(|error| format!("failed to serialize Python preparation: {error}"))?;
        let response = self
            .bridge
            .call1_string(c"prepare", &request)?
            .ok_or_else(|| "Python preparation bridge returned no response".to_string())?;
        serde_json::from_str(&response)
            .map_err(|error| format!("invalid Python preparation response: {error}"))
    }
}

// Called once for a fresh selection, before the adapter changes the worker
// environment. The adapter retains these embedding fields for attachment.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_inspect_python(python: SEXP) -> harp::Result<SEXP> {
    let python = String::try_from(harp::object::RObject::view(python))?;
    let selected = crate::worker::inspect_python(std::path::Path::new(&python))
        .map_err(|error| harp::anyhow!("{error}"))?;
    let result = serde_json::to_string(&selected).map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(result).sexp)
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
    let selected = SelectedPython {
        python,
        libpython,
        python_home,
    };
    let rust_owned =
        super::initialize_selected(&selected).map_err(|error| harp::anyhow!("{error}"))?;
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

// Reticulate calls this after installing its own stream and input hooks.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_install_python_services(
    libpython: SEXP,
) -> harp::Result<SEXP> {
    let libpython = String::try_from(harp::object::RObject::view(libpython))?;
    super::startup::install_services(std::path::Path::new(&libpython))
        .map_err(|error| harp::anyhow!("{error}"))?;
    unsafe { Ok(libr::R_NilValue) }
}

// Reticulate converts the optional R resolver callback. Native startup owns
// the installation and CPython call; no reticulate string dispatcher runs it.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_setup_python_runtime(
    libpython: SEXP,
    callback: SEXP,
    disabled_reason: SEXP,
) -> harp::Result<SEXP> {
    let libpython = Option::<String>::try_from(harp::object::RObject::view(libpython))?
        .ok_or_else(|| harp::anyhow!("Python-hosted R is not supported"))?;
    let disabled_reason = if unsafe { disabled_reason == libr::R_NilValue } {
        None
    } else {
        Some(String::try_from(harp::object::RObject::view(
            disabled_reason,
        ))?)
    };
    let callback = reticulate_callback(callback)?;
    let completed = super::setup_runtime(
        std::path::Path::new(&libpython),
        super::ImportResolution {
            callback,
            disabled_reason: disabled_reason.as_deref(),
        },
    )
    .map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(completed).sexp)
}

fn reticulate_callback(callback: SEXP) -> harp::Result<Option<std::ptr::NonNull<libc::c_void>>> {
    // A converted reticulate callable is an R function with a `py_object`
    // reference environment. Keep this adapter-specific representation here.
    // The .Call argument retains that wrapper for the CPython configuration
    // call; the finder retains the resulting Python callback afterwards.
    unsafe {
        if callback == libr::R_NilValue {
            return Ok(None);
        }
        let reference = libr::Rf_getAttrib(callback, libr::Rf_install(c"py_object".as_ptr()));
        if libr::TYPEOF(reference) != libr::ENVSXP as i32 {
            return Err(harp::anyhow!("reticulate callback has no Python reference"));
        }
        let pointer = libr::Rf_findVarInFrame(reference, libr::Rf_install(c"pyobj".as_ptr()));
        if libr::TYPEOF(pointer) != libr::EXTPTRSXP as i32 {
            return Err(harp::anyhow!("reticulate callback has no Python object"));
        }
        std::ptr::NonNull::new(libr::R_ExternalPtrAddr(pointer))
            .map(Some)
            .ok_or_else(|| harp::anyhow!("reticulate callback Python object is unavailable"))
    }
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
    super::finish_initialization().map_err(|error| harp::anyhow!("{error}"))?;
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

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_resolve_python(request: SEXP) -> harp::Result<SEXP> {
    let request = String::try_from(harp::object::RObject::view(request))?;
    let request = serde_json::from_str(&request).map_err(|error| harp::anyhow!("{error}"))?;
    let python =
        crate::worker::resolve_python(request).map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(python).sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_resolve_python_version(request: SEXP) -> harp::Result<SEXP> {
    let request = String::try_from(harp::object::RObject::view(request))?;
    let request = serde_json::from_str(&request).map_err(|error| harp::anyhow!("{error}"))?;
    let version =
        crate::worker::resolve_python_version(request).map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(version).sexp)
}
