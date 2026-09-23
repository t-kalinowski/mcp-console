//! Public R representation of the native environment owner's requirements.
//!
//! Field presence, vector attributes, ordering, duplicates, and history belong
//! to this projection. Resolution and activation use `environment` exclusively.

use std::cell::RefCell;

use harp::object::RObject;
use libr::SEXP;

thread_local! {
    // R objects and their GC protection stay on the R thread.
    static REQUIREMENTS: RefCell<Option<RObject>> = const { RefCell::new(None) };
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_requirements_get() -> harp::Result<SEXP> {
    let value = REQUIREMENTS
        .with_borrow(|value| value.as_ref().map(|value| value.sexp))
        .ok_or_else(|| harp::anyhow!("managed Python requirements are not installed"))?;
    // Allocation may run finalizers; leave the borrow before copying.
    let value = RObject::new(value);
    Ok(value.duplicate().sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_requirements_set(value: SEXP) -> harp::Result<SEXP> {
    let value = RObject::view(value).duplicate();
    let previous = REQUIREMENTS.with_borrow_mut(|current| current.replace(value));
    // Release protection outside the state borrow.
    drop(previous);
    unsafe { Ok(libr::R_NilValue) }
}
