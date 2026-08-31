use libr::SEXP;

use super::PreparationOutcome;

const PYTHON_BRIDGE_SOURCE: &str = include_str!("bridge.R");
const PYTHON_INITIALIZER_SOURCE: &str = include_str!("initialize.R");

/// The current Python backend, hosted by reticulate inside embedded R.
pub(super) struct Runtime(crate::r_bridge::Bridge);

pub(super) fn configure_worker_environment() -> std::io::Result<()> {
    super::platform::set_environment(c"RETICULATE_REMAP_OUTPUT_STREAMS", c"1", true)
}

impl Runtime {
    pub(super) fn initialize() -> Result<Self, String> {
        let source = format!(
            "base::local(\n  {{\n    state <- ({PYTHON_BRIDGE_SOURCE})\n{PYTHON_INITIALIZER_SOURCE}\n    state\n  }},\n  envir = base::new.env(parent = base::baseenv())\n)"
        );
        crate::r_bridge::Bridge::initialize(&source, "Python").map(Self)
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

// Release the initial GIL when control leaves reticulate's C initializer,
// including its error paths. Later reticulate calls acquire the GIL normally.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_finish_python_initialization() -> harp::Result<SEXP> {
    super::library::finish_initialization().map_err(|error| harp::anyhow!("{error}"))?;
    unsafe { Ok(libr::R_NilValue) }
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
