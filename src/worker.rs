#[cfg(any(unix, windows))]
mod coordinator;
#[cfg(any(unix, windows))]
mod core;
#[cfg(any(unix, windows))]
mod embedded_r;
#[cfg(any(unix, windows))]
mod input;

// Keep the rest of the crate dependent on the worker facade. The core owns
// runtime-neutral sideband state and host callbacks. The coordinator owns
// language dispatch; the R backend owns its interpreter and native events.
#[cfg(any(unix, windows))]
pub(crate) use coordinator::run;
#[cfg(any(unix, windows))]
pub(crate) use core::{
    publish_plot, publish_python_activation, publish_r_activation, publish_r_activation_failure,
    resolve_python, resolve_python_version,
};
#[cfg(any(unix, windows))]
pub(crate) use embedded_r::resolve_r;

#[cfg(not(any(unix, windows)))]
pub(crate) fn run() -> Result<(), Box<dyn std::error::Error>> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "embedded R workers are supported only on macOS",
    )
    .into())
}
