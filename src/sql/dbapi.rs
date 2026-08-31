use libr::SEXP;

const PYTHON_RUNTIME_SOURCE: &str = include_str!("dbapi.py");

pub(super) enum Provider {
    R,
    Managed,
    Python,
}

pub(super) fn install_runtime() -> Result<(), String> {
    crate::python::install_sql_runtime(PYTHON_RUNTIME_SOURCE)
}

pub(super) fn dispatch(source: &str) -> Result<Provider, String> {
    match crate::python::dispatch_sql(source)? {
        crate::python::SqlProvider::R => Ok(Provider::R),
        crate::python::SqlProvider::Managed => Ok(Provider::Managed),
        crate::python::SqlProvider::Python => Ok(Provider::Python),
    }
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_sql_use_r() -> harp::Result<SEXP> {
    crate::python::use_r_sql().map_err(|error| harp::anyhow!("{error}"))?;
    unsafe { Ok(libr::R_NilValue) }
}
