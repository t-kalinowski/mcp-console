/// The unsupported platform runtime keeps the server boundary portable.
pub(super) struct WorkerRuntime;

pub(super) struct Worker;

impl WorkerRuntime {
    pub(super) fn spawn(
        &self,
        spec: super::WorkerSpec<'_>,
        _output: super::OutputTape,
        _on_started: impl FnOnce(WorkerShutdownHandle) -> Result<(), String>,
    ) -> Result<Worker, String> {
        let super::WorkerSpec {
            executable,
            arguments,
            managed_python,
            managed_r,
        } = spec;
        let _ = (executable, arguments, managed_python, managed_r);
        Err("workers are supported only on macOS".to_string())
    }
}

impl Worker {
    pub(super) fn prepare_r(
        &mut self,
        _library: &std::path::Path,
    ) -> Result<Result<(), String>, String> {
        unreachable!("unsupported workers cannot start")
    }

    pub(super) fn prepare_python(
        &mut self,
        packages: Vec<String>,
        _resolve_python: impl FnMut(
            crate::worker_protocol::PythonResolveRequest,
        ) -> Result<crate::resolver::ManagedPython, String>,
    ) -> Result<
        Result<
            (
                crate::worker_protocol::PythonRequirementManifest,
                Vec<crate::resolver::ManagedPython>,
            ),
            String,
        >,
        String,
    > {
        let _ = packages;
        unreachable!("unsupported workers cannot start")
    }

    pub(super) fn evaluate(
        &mut self,
        cell: crate::cell::Cell,
        _evaluation: &super::Evaluation,
        _resolve_python: impl FnMut(
            crate::worker_protocol::PythonResolveRequest,
        ) -> Result<crate::resolver::ManagedPython, String>,
        _checkpoint_python: impl FnMut(
            Option<crate::worker_protocol::PythonRequirementManifest>,
            Vec<crate::resolver::ManagedPython>,
        ) -> Result<(), String>,
    ) -> Result<(), String> {
        let crate::cell::Cell { language, source } = cell;
        let _ = (language, source);
        unreachable!("unsupported workers cannot start")
    }

    pub(super) fn write_stdin(&self, _stdin: String) -> Result<(), String> {
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
    pub(super) fn shutdown(
        &self,
        _deadline: std::time::Instant,
    ) -> Result<std::thread::JoinHandle<()>, String> {
        Ok(std::thread::spawn(|| {}))
    }
}
