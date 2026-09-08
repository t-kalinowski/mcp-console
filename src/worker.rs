#[cfg(unix)]
mod core;
#[cfg(unix)]
mod embedded_r;

// Keep the rest of the crate dependent on the worker facade. The core owns
// runtime-neutral sideband state and host callbacks, while the embedded-R
// backend owns R initialization, event handling, and language dispatch.
#[cfg(unix)]
pub(crate) use core::{
    publish_plot, publish_python_activation, publish_r_activation, publish_r_activation_failure,
    resolve_python, resolve_python_version,
};
#[cfg(unix)]
pub(crate) use embedded_r::{resolve_r, run};

#[cfg(not(unix))]
pub(crate) fn run() -> Result<(), Box<dyn std::error::Error>> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "embedded R workers are supported only on macOS",
    )
    .into())
}
