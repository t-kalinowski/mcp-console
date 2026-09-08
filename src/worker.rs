mod core;
mod embedded_r;

// Keep the rest of the crate dependent on the worker facade. The core owns
// runtime-neutral sideband state and host callbacks, while the embedded-R
// backend owns R initialization, event handling, and language dispatch.
pub(crate) use core::{
    publish_plot, publish_python_activation, publish_r_activation, publish_r_activation_failure,
    resolve_python, resolve_python_version,
};
pub(crate) use embedded_r::{resolve_r, run};
