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
        state
            .requirements
            .pending_activation
            .clone()
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
        (previous, state.requirements.commit(value))
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
    let pending = STATE.with(|state| state.borrow().requirements.activation_pending());
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
        state.requirements.pending_activation = Some(activation);
        state.pending_metadata.replace(Rc::new(metadata))
    });
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

#[allow(clippy::result_large_err)]
pub(in crate::python::requirements) fn check_activation() -> harp::Result<()> {
    STATE
        .with(|state| state.borrow().requirements.check_activation())
        .map_err(|error| harp::anyhow!("{error}"))
}

#[allow(clippy::result_large_err)]
fn publish_activation(activation: SEXP) -> harp::Result<()> {
    // The bridge still supplies its normalized projection in this order.
    let activation = RObject::view(activation);
    let requirements = crate::worker_protocol::PythonRequirementManifest {
        packages: activation.vector_elt(0)?.try_into()?,
        python_version: activation.vector_elt(1)?.try_into()?,
        exclude_newer: r_null_or_try_into(activation.vector_elt(2)?)?,
    };
    crate::worker::publish_python_activation(requirements).map_err(|error| harp::anyhow!("{error}"))
}

impl Adapter {
    pub(in crate::python::requirements) fn activate(
        &self,
        python: &Value,
        candidate: &Record,
    ) -> super::super::Result<Value> {
        let config = Record::config(self.call("candidate_config", &[python])?)?;
        let candidate_python = python.text()?;
        let candidate_libpython = config.get("libpython")?.text()?;
        let candidate_executable = config.get("executable")?.text()?;
        let running_libpython = self.call("live_libpython", &[])?.text()?;
        let input = crate::python::ActivationInput {
            candidate_python: &candidate_python,
            candidate_libpython: &candidate_libpython,
            candidate_executable: &candidate_executable,
            running_libpython: &running_libpython,
        };
        match crate::python::activate_managed_environment(input) {
            Ok(()) => {}
            Err(crate::python::ActivationFailure::PythonException) => {
                // CPython retained the original exception and traceback. Let
                // reticulate translate it through the existing condition and
                // interrupt boundary; do not print it in the native operation.
                self.call("raise_python_setup_error", &[])?;
                return Err("Python activation failed without an exception".into());
            }
            Err(error) => return Err(error.to_string().into()),
        }

        let config = self.call("available_config", &[config.value()])?;
        // The active-binding write commits requirements and publishes the
        // activation only after reticulate accepts this returned config.
        self.call("record_activation", &[candidate.value()])?;
        Ok(config)
    }
}
