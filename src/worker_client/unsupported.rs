/// The unsupported platform runtime keeps the server boundary portable.
pub(super) struct WorkerRuntime;

pub(super) struct Worker;

impl WorkerRuntime {
    pub(super) fn spawn(
        &self,
        spec: super::WorkerSpec<'_>,
        _output: super::OutputTape,
        _on_started: impl FnOnce(WorkerShutdownHandle) -> Result<(), String>,
        _on_ready: impl FnOnce() -> Result<(), String>,
    ) -> Result<Worker, String> {
        let super::WorkerSpec {
            executable,
            arguments,
            relay,
            managed_python,
            managed_r,
            callbacks,
        } = spec;
        let _ = (
            executable,
            arguments,
            relay,
            managed_python,
            managed_r,
            callbacks,
        );
        Err("workers are supported only on macOS".to_string())
    }
}

impl Worker {
    pub(super) fn prepare_r(
        &mut self,
        _library: &std::path::Path,
    ) -> Result<super::TerminalCommit<Result<(), String>>, String> {
        unreachable!("unsupported workers cannot start")
    }

    pub(super) fn prepare_python(
        &mut self,
        packages: Vec<String>,
    ) -> Result<super::TerminalCommit<Result<Option<crate::resolver::ManagedPython>, String>>, String>
    {
        let _ = packages;
        unreachable!("unsupported workers cannot start")
    }

    pub(super) fn evaluate(
        &mut self,
        cell: crate::cell::Cell,
        _evaluation: std::sync::Arc<super::Evaluation>,
    ) -> Result<super::TerminalCommit<super::output::OutputCheckpoint>, String> {
        let _ = cell;
        unreachable!("unsupported workers cannot start")
    }

    pub(super) fn write_stdin(&self, _stdin: String) -> Result<(), String> {
        unreachable!("unsupported workers cannot start")
    }

    pub(super) fn snapshot(
        &self,
        _output: &super::OutputTape,
    ) -> Result<super::WorkerSnapshot, String> {
        unreachable!("unsupported workers cannot start")
    }

    pub(super) fn shutdown(&mut self, _deadline: std::time::Instant) -> Result<(), String> {
        Ok(())
    }

    pub(super) fn finish_retirement(&mut self) -> Result<(), String> {
        Ok(())
    }

    pub(super) fn shutdown_handle(&self) -> WorkerShutdownHandle {
        WorkerShutdownHandle
    }
}

#[derive(Clone)]
pub(super) struct WorkerShutdownHandle;

impl WorkerShutdownHandle {
    pub(super) fn interrupt(&self) -> Result<(), String> {
        Err("worker interrupts are supported only on macOS".to_string())
    }

    pub(super) fn shutdown(
        &self,
        _deadline: std::time::Instant,
    ) -> Result<std::thread::JoinHandle<()>, String> {
        Ok(std::thread::spawn(|| {}))
    }
}
