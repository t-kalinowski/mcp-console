use libr::SEXP;

use super::PreparationOutcome;

const PYTHON_BRIDGE_SOURCE: &str = include_str!("bridge.R");
const PYTHON_RUNTIME_SOURCE: &str = include_str!("runtime.py");

/// The current Python backend, hosted by reticulate inside embedded R.
pub(super) struct Runtime(crate::r_bridge::Bridge);

pub(super) fn configure_worker_environment() -> std::io::Result<()> {
    super::platform::set_environment(c"RETICULATE_REMAP_OUTPUT_STREAMS", c"1", true)
}

impl Runtime {
    pub(super) fn initialize() -> Result<Self, String> {
        crate::r_bridge::Bridge::initialize(PYTHON_BRIDGE_SOURCE, "Python").map(Self)
    }

    pub(super) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        self.0.evaluate(source)
    }

    pub(super) fn prepare(&self, packages: Vec<String>) -> Result<PreparationOutcome, String> {
        let request = serde_json::to_string(&packages)
            .map_err(|error| format!("failed to serialize Python preparation: {error}"))?;
        let response = self
            .0
            .call1_string(c"prepare", &request)?
            .ok_or_else(|| "Python preparation bridge returned no response".to_string())?;
        serde_json::from_str(&response)
            .map_err(|error| format!("invalid Python preparation response: {error}"))
    }
}

// Reticulate calls this with the exact library selected for initialization.
// Rust owns the process-lifetime handle; reticulate still initializes CPython.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_load_python_library(path: SEXP) -> harp::Result<SEXP> {
    let path = String::try_from(harp::object::RObject::view(path))?;
    super::library::load(std::path::Path::new(&path))
        .map_err(|error| harp::anyhow!("{error}"))?;
    unsafe { Ok(libr::R_NilValue) }
}

// The process-lifetime R bridge calls this once during initialization,
// before any reticulate hook can initialize Python.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_runtime_source() -> harp::Result<SEXP> {
    Ok(harp::object::RObject::from(PYTHON_RUNTIME_SOURCE).sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_publish_python_plot(data: SEXP) -> harp::Result<SEXP> {
    let data = String::try_from(harp::object::RObject::view(data))?;
    crate::worker::publish_plot(Ok(data));
    unsafe { Ok(libr::R_NilValue) }
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
pub extern "C-unwind" fn mcp_console_python_activated(requirements: SEXP) -> harp::Result<SEXP> {
    let requirements = String::try_from(harp::object::RObject::view(requirements))?;
    let requirements =
        serde_json::from_str(&requirements).map_err(|error| harp::anyhow!("{error}"))?;
    crate::worker::publish_python_activation(requirements)
        .map_err(|error| harp::anyhow!("{error}"))?;
    unsafe { Ok(libr::R_NilValue) }
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
