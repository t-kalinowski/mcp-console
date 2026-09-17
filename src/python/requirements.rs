//! Passive storage for the managed worker's current requirement object.
//!
//! Keep the R representation, including field presence, vector attributes and
//! history. Reticulate still decides transitions; the existing R bridge alone
//! reads and writes this value. Storing a declaration does not publish activation.

use std::cell::RefCell;

use harp::object::RObject;
use libr::SEXP;

thread_local! {
    // R objects and their GC protection stay on the R thread. Each worker
    // process installs its own value through the managed startup bridge.
    static CURRENT: RefCell<Option<RObject>> = const { RefCell::new(None) };
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_requirements_get() -> harp::Result<SEXP> {
    let value = CURRENT
        .with(|current| current.borrow().as_ref().map(|value| value.sexp))
        .ok_or_else(|| harp::anyhow!("managed Python requirements are not installed"))?;
    // Allocate outside the state borrow: R allocation can run finalizers.
    // Copies keep ordinary R edits to returned objects out of the stored value.
    let value = RObject::new(value);
    Ok(value.duplicate().sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_requirements_set(value: SEXP) -> harp::Result<SEXP> {
    let value = RObject::view(value).duplicate();
    CURRENT.with(|current| *current.borrow_mut() = Some(value));
    unsafe { Ok(libr::R_NilValue) }
}
