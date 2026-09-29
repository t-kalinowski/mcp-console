//! Reticulate's configuration and publication side of live activation.

use std::rc::Rc;

use harp::object::{RObject, is_identical, r_null_or_try_into};
use libr::SEXP;

use super::{Adapter, Metadata, Record, STATE, Value};

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_requirements_set(
    value: SEXP,
    activation: SEXP,
) -> harp::Result<SEXP> {
    let pending = STATE.with(|state| {
        let state = state.borrow();
        super::super::STATE
            .with(|state| state.borrow().pending_activation.clone())
            .zip(state.pending_metadata.clone())
    });
    if let Some((pending, metadata)) = pending
        && !is_identical(activation, metadata.to_r(&pending)?.sexp)
    {
        return Err(harp::anyhow!(
            "Python requirement update does not match pending activation"
        ));
    }
    let (value, metadata) = Metadata::from_r(value)?;
    let (previous, committed) = STATE.with(|state| {
        let mut state = state.borrow_mut();
        let previous = (
            state.current_metadata.replace(Rc::new(metadata)),
            state.pending_metadata.take(),
        );
        (
            previous,
            super::super::STATE.with(|state| state.borrow_mut().commit(value)),
        )
    });
    // Release protection and publish only after leaving the state borrow.
    drop(previous);
    if committed {
        publish_activation(activation)?;
    }
    unsafe { Ok(libr::R_NilValue) }
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_activation_pending() -> harp::Result<SEXP> {
    let pending = super::super::STATE.with(|state| state.borrow().activation_pending());
    Ok(RObject::from(pending).sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_activation_record(
    activation: SEXP,
) -> harp::Result<SEXP> {
    check_activation()?;
    // Called only after native activation and process-environment setup
    // succeed. An earlier failure leaves ordinary snapshot restoration inert.
    let (activation, metadata) = Metadata::from_r(activation)?;
    let previous = STATE.with(|state| {
        let mut state = state.borrow_mut();
        super::super::STATE.with(|state| state.borrow_mut().pending_activation = Some(activation));
        state.pending_metadata.replace(Rc::new(metadata))
    });
    drop(previous);
    unsafe { Ok(libr::R_NilValue) }
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_initialized(activation: SEXP) -> harp::Result<SEXP> {
    // Initial startup has its own successful reticulate hook, with no pending
    // late activation or subsequent requirement write. R selection hints can
    // select an inspected interpreter without a managed resolver candidate.
    let requirements = activation_requirements(activation)?;
    let selected = crate::python::library::selected_configuration()
        .map_err(|error| harp::anyhow!("{error}"))?;
    if !super::super::initialized() {
        super::super::initialize(&selected, requirements)
            .map_err(|error| harp::anyhow!("{error}"))?;
    }
    unsafe { Ok(libr::R_NilValue) }
}

#[allow(clippy::result_large_err)]
pub(in crate::python::requirements) fn check_activation() -> harp::Result<()> {
    super::super::STATE
        .with(|state| state.borrow().check_activation())
        .map_err(|error| harp::anyhow!("{error}"))
}

#[allow(clippy::result_large_err)]
fn activation_requirements(
    activation: SEXP,
) -> harp::Result<crate::worker_protocol::PythonRequirementManifest> {
    // The bridge still supplies its normalized projection in this order.
    let activation = RObject::view(activation);
    Ok(crate::worker_protocol::PythonRequirementManifest {
        packages: activation.vector_elt(0)?.try_into()?,
        python_version: activation.vector_elt(1)?.try_into()?,
        exclude_newer: r_null_or_try_into(activation.vector_elt(2)?)?,
    })
}

#[allow(clippy::result_large_err)]
fn publish_activation(activation: SEXP) -> harp::Result<()> {
    let requirements = activation_requirements(activation)?;
    super::super::accept(requirements.clone()).map_err(|error| harp::anyhow!("{error}"))?;
    crate::worker::publish_python_activation(requirements).map_err(|error| harp::anyhow!("{error}"))
}

impl Adapter {
    pub(in crate::python::requirements) fn activate(
        &self,
        python: &Value,
        candidate: &Record,
    ) -> super::super::RResult<Value> {
        let python = python.text()?;
        let selected = super::super::resolved_selection()
            .filter(|selected| python == selected.embedding.python)
            .ok_or("Python activation has no matching inspected selection")?;
        super::super::validate_selected(&selected).map_err(|error| error.to_string())?;
        let encoded = serde_json::to_string(&selected).map_err(|error| error.to_string())?;
        let encoded =
            harp::exec::r_sandbox(|| Value(RObject::from(encoded))).map_err(super::from_r_error)?;
        let config = Record::config(self.call("activation_config", &[&encoded])?)?;
        match super::super::activate_selected(&selected) {
            Ok(()) => {}
            Err(crate::python::ActivationFailure::PythonException) => {
                super::super::activation_failed()?;
                // CPython retained the original exception and traceback. Let
                // reticulate translate it through the existing condition and
                // interrupt boundary; do not print it in the native operation.
                self.call("raise_python_setup_error", &[])?;
                return Err("Python activation failed without an exception".into());
            }
            Err(error) => {
                if matches!(error, crate::python::ActivationFailure::Infrastructure(_)) {
                    super::super::activation_failed()?;
                }
                return Err(error.to_string().into());
            }
        }
        let result = (|| {
            let config = self.call("available_config", &[config.value()])?;
            // The active-binding write publishes only after reticulate accepts
            // this returned config. A failed projection cannot undo activation.
            self.call("record_activation", &[candidate.value()])?;
            Ok(config)
        })();
        if result.is_err() {
            super::super::activation_failed()?;
        }
        result
    }
}
