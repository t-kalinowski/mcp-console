use libr::SEXP;

const BRIDGE_SOURCE: &str = include_str!("bridge.R");

/// Optional conversion and cross-language calls. Ordinary Python cells never
/// enter this adapter, and registering its R hooks does not initialize Python.
pub(crate) struct Interop {
    _state: crate::r_bridge::Bridge,
}

impl Interop {
    pub(crate) fn initialize() -> Result<Self, String> {
        crate::r_bridge::Bridge::initialize(BRIDGE_SOURCE, "Python interoperability")
            .map(|state| Self { _state: state })
    }
}

pub(super) fn attach() -> Result<(), String> {
    harp::parse_eval_base("invisible(reticulate::import_main(convert = FALSE))")
        .map(|_| ())
        .map_err(|error| error.to_string())
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_state() -> harp::Result<SEXP> {
    let state = super::native::state().map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(state.to_string()).sexp)
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

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_initialize() -> harp::Result<SEXP> {
    super::native::ensure_initialized().map_err(|error| harp::anyhow!("{error}"))?;
    let state = super::native::state().map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(state.to_string()).sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_prepare(request: SEXP) -> harp::Result<SEXP> {
    let request = String::try_from(harp::object::RObject::view(request))?;
    let request = serde_json::from_str(&request).map_err(|error| harp::anyhow!("{error}"))?;
    let response = super::native::declare(request).map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(response.to_string()).sexp)
}
