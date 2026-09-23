#[cfg(unix)]
mod coordinator;
#[cfg(unix)]
mod core;
#[cfg(unix)]
mod embedded_r;
#[cfg(unix)]
mod input;
#[cfg(unix)]
mod interrupt;

// Keep the rest of the crate dependent on the worker facade. The core owns
// runtime-neutral sideband state and host callbacks. The coordinator owns
// language dispatch; the R backend owns its interpreter and native events.
#[cfg(unix)]
pub(crate) use coordinator::run;
#[cfg(unix)]
pub(crate) use core::{
    emit_output, mark_shutting_down, publish_plot, publish_python_activation, publish_r_activation,
    publish_r_activation_failure, record_worker_failure, resolve_python, resolve_python_version,
    resolve_r,
};
#[cfg(unix)]
pub(crate) use embedded_r::{begin_python_commit, finish_python_commit};
#[cfg(unix)]
pub(crate) use input::{PythonInput, python_interrupt_wakeup, read_python_input};
#[cfg(unix)]
pub(crate) use interrupt::{
    acknowledge_python_interrupt, install_python_interrupt, pending as python_interrupt_pending,
};

#[cfg(not(unix))]
pub(crate) fn run() -> Result<(), Box<dyn std::error::Error>> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "embedded R workers are supported only on macOS",
    )
    .into())
}
