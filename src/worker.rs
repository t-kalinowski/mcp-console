#[cfg(target_os = "macos")]
mod core;
#[cfg(target_os = "macos")]
mod embedded_r;

// Keep the rest of the crate dependent on the worker facade. The core owns
// runtime-neutral sideband state and host callbacks, while the embedded-R
// backend owns R initialization, event handling, and language dispatch.
#[cfg(target_os = "macos")]
pub(crate) use core::{
    publish_plot, publish_python_activation, publish_r_activation, publish_r_activation_failure,
    resolve_python, resolve_python_version, resolve_r,
};
#[cfg(target_os = "macos")]
pub(crate) use embedded_r::run;

#[cfg(not(target_os = "macos"))]
pub(crate) fn run() -> Result<(), Box<dyn std::error::Error>> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "embedded R workers are supported only on macOS",
    )
    .into())
}
