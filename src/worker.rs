#[cfg(unix)]
mod coordinator;
#[cfg(unix)]
mod core;
#[cfg(unix)]
mod embedded_r;
#[cfg(unix)]
mod input;
#[cfg(unix)]
pub(crate) mod interrupt;
#[cfg(unix)]
mod process;

// Keep the rest of the crate dependent on the worker facade. The core owns
// runtime-neutral sideband state and host callbacks. The coordinator owns
// language dispatch; the R backend owns its interpreter and native events.
#[cfg(unix)]
pub(crate) use coordinator::run;
#[cfg(unix)]
pub(crate) use core::{
    emit_output, publish_plot, publish_python_activation, publish_r_activation,
    publish_r_activation_failure, resolve_python, resolve_python_version,
};
#[cfg(unix)]
pub(crate) use embedded_r::{require_available as require_r, resolve_r};

#[cfg(not(unix))]
pub(crate) fn run() -> Result<(), Box<dyn std::error::Error>> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "embedded R workers are supported only on macOS",
    )
    .into())
}

#[cfg(unix)]
pub(crate) use input::read_line;
#[cfg(unix)]
pub(crate) use process::output as process_output;

#[cfg(unix)]
pub(crate) fn emit_diagnostic(message: &str) {
    core::emit_output(
        crate::worker_protocol::ConsoleChannel::Diagnostic,
        message.as_bytes(),
    );
}
