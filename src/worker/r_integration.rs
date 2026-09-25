use super::{core, embedded_r, interrupt};

// The ordinary worker installs R during startup. Keeping its event and
// graphics hooks optional lets the coordinator serve its native lifecycle
// without loading an interpreter.
pub(super) struct Integration {
    r: Option<embedded_r::Runtime>,
}

impl Integration {
    pub(super) fn new(r: Option<embedded_r::Runtime>) -> std::io::Result<Self> {
        if r.is_none() {
            interrupt::normalize_signal()?;
            interrupt::initialize_native()?;
        }
        Ok(Self { r })
    }

    pub(super) fn wait_for_activity(&self, sideband_fd: libc::c_int) -> Result<bool, String> {
        if self.r.is_some() {
            embedded_r::wait_for_activity(sideband_fd)
        } else {
            interrupt::wait_for_activity(sideband_fd)
        }
    }

    pub(super) fn idle(&self) -> Result<(), String> {
        if let Some(r) = &self.r {
            r.idle()
        } else {
            // Consume idle SIGINT before admitting another cell. CPython's
            // pending hook then sees acknowledged state and returns normally.
            interrupt::acknowledge_python_interrupt();
            core::observe_stdin_shutdown()
        }
    }

    pub(super) fn check_interrupts(&self) {
        if self.r.is_some() {
            embedded_r::check_interrupts();
        }
    }

    pub(super) fn prepare_python<T>(
        &self,
        operation: impl FnOnce() -> Result<T, String>,
    ) -> Result<T, String> {
        if self.r.is_some() {
            embedded_r::defer_interrupts(operation, embedded_r::discard_interrupts)
        } else {
            let result = operation();
            interrupt::acknowledge_python_interrupt();
            result
        }
    }

    pub(super) fn begin_graphics(&self) -> Result<(), String> {
        if let Some(r) = &self.r {
            r.begin_graphics()
        } else {
            Ok(())
        }
    }

    pub(super) fn finish_graphics(&self) -> Result<(), String> {
        if let Some(r) = &self.r {
            r.finish_graphics()
        } else {
            Ok(())
        }
    }

    pub(super) fn evaluate_r(&self, source: String) -> Result<(), String> {
        self.r
            .as_ref()
            .expect("R integration installed")
            .evaluate(source)
    }

    pub(super) fn prepare_r(
        &self,
        library: &str,
    ) -> Result<crate::r_environment::PreparationOutcome, String> {
        self.r
            .as_ref()
            .expect("R integration installed")
            .prepare(library)
    }
}
