//! Managed worker requirements and activation commits.
//!
//! Keep the R representation, including field presence, vector attributes and
//! history. Reticulate still calculates transitions and activates environments.
//! The bridge supplies normalized activation keys, but this owner decides when
//! a requirement write commits an activation and publishes its notification.

use std::cell::RefCell;

use harp::object::{RObject, is_identical, r_null_or_try_into};
use libr::SEXP;

struct Requirements {
    current: Option<RObject>,
    // A transient matching key for an environment already activated by
    // reticulate, not a second copy of the authoritative requirement object.
    pending_activation: Option<RObject>,
}

thread_local! {
    // R objects and their GC protection stay on the R thread. Each worker
    // process installs its own value through the managed startup bridge.
    static REQUIREMENTS: RefCell<Requirements> = const { RefCell::new(Requirements {
        current: None,
        pending_activation: None,
    }) };
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_requirements_get() -> harp::Result<SEXP> {
    let value = REQUIREMENTS
        .with(|state| state.borrow().current.as_ref().map(|value| value.sexp))
        .ok_or_else(|| harp::anyhow!("managed Python requirements are not installed"))?;
    // Allocate outside the state borrow: R allocation can run finalizers.
    // Copies keep ordinary R edits to returned objects out of the stored value.
    let value = RObject::new(value);
    Ok(value.duplicate().sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_requirements_set(
    value: SEXP,
    activation: SEXP,
) -> harp::Result<SEXP> {
    let pending = REQUIREMENTS.with(|state| {
        state
            .borrow()
            .pending_activation
            .as_ref()
            .map(|value| value.sexp)
    });
    let pending = pending.map(RObject::new);
    if let Some(pending) = &pending
        && !is_identical(activation, pending.sexp)
    {
        return Err(harp::anyhow!(
            "Python requirement update does not match pending activation"
        ));
    }
    let value = RObject::view(value).duplicate();
    let (previous, committed) = REQUIREMENTS.with(|state| {
        let mut state = state.borrow_mut();
        (
            state.current.replace(value),
            state.pending_activation.take(),
        )
    });
    // Release protection and publish only after leaving the state borrow.
    drop(previous);
    if committed.is_some() {
        publish_activation(activation)?;
    }
    unsafe { Ok(libr::R_NilValue) }
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_activation_pending() -> harp::Result<SEXP> {
    Ok(RObject::from(activation_pending()).sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_activation_check() -> harp::Result<SEXP> {
    check_activation()?;
    unsafe { Ok(libr::R_NilValue) }
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_activation_record(
    activation: SEXP,
) -> harp::Result<SEXP> {
    check_activation()?;
    // Called only after reticulate activation and process-environment setup
    // succeed. An earlier failure leaves ordinary snapshot restoration inert.
    let activation = RObject::view(activation).duplicate();
    let previous =
        REQUIREMENTS.with(|state| state.borrow_mut().pending_activation.replace(activation));
    drop(previous);
    unsafe { Ok(libr::R_NilValue) }
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_initialized(activation: SEXP) -> harp::Result<SEXP> {
    // Initial startup has its own successful reticulate hook, with no pending
    // late activation or subsequent requirement write to commit it.
    publish_activation(activation)?;
    unsafe { Ok(libr::R_NilValue) }
}

fn activation_pending() -> bool {
    REQUIREMENTS.with(|state| state.borrow().pending_activation.is_some())
}

#[allow(clippy::result_large_err)]
fn check_activation() -> harp::Result<()> {
    if activation_pending() {
        return Err(harp::anyhow!(
            "Python activation is awaiting a requirement update"
        ));
    }
    Ok(())
}

#[allow(clippy::result_large_err)]
fn publish_activation(activation: SEXP) -> harp::Result<()> {
    // The bridge's existing manifest projection supplies these three fields
    // in order. It preserves R's normalization and matching semantics without
    // changing the stored requirement object's representation.
    let activation = RObject::view(activation);
    let requirements = crate::worker_protocol::PythonRequirementManifest {
        packages: activation.vector_elt(0)?.try_into()?,
        python_version: activation.vector_elt(1)?.try_into()?,
        exclude_newer: r_null_or_try_into(activation.vector_elt(2)?)?,
    };
    crate::worker::publish_python_activation(requirements).map_err(|error| harp::anyhow!("{error}"))
}
