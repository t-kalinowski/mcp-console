use std::sync::Arc;
use std::time::{Duration, Instant};

use super::environment::merge_python_requirements;
use super::output::{Response, SendFailure};
use super::{Client, WorkerState, platform};

/// Identifies work admitted against one worker without exposing an epoch counter.
#[derive(Clone)]
pub(super) struct WorkerGeneration(Arc<()>);

impl WorkerGeneration {
    fn new() -> Self {
        Self(Arc::new(()))
    }

    pub(super) fn is(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.0, &other.0)
    }
}

/// Owns admission and process cancellation for the implicit session.
pub(super) struct LifecycleControl {
    pub(super) state: LifecycleState,
    pub(super) generation: WorkerGeneration,
    pub(super) processes: ProcessStopHandles,
}

impl LifecycleControl {
    pub(super) fn new() -> Self {
        Self {
            state: LifecycleState::Ready,
            generation: WorkerGeneration::new(),
            processes: ProcessStopHandles::default(),
        }
    }

    fn start_restart(&mut self, grace: Duration) -> (ProcessStopHandles, Instant) {
        let deadline = Instant::now() + grace;
        let stop_handles = self.processes.clone();
        self.state = LifecycleState::Restarting { deadline };
        self.generation = WorkerGeneration::new();
        self.processes.resolver = None;
        (stop_handles, deadline)
    }
}

#[derive(Clone, Copy, Eq, PartialEq)]
/// Carries a deadline only while processes are being stopped.
pub(super) enum LifecycleState {
    Ready,
    Restarting { deadline: Instant },
    ShuttingDown { deadline: Instant },
}

pub(super) enum GenerationStatus {
    CurrentReady,
    CurrentClosing,
    Changed,
}

enum RestartOutcome {
    Restarted(Response),
    Failed(SendFailure),
    TerminalFailure(SendFailure),
}

#[derive(Clone, Default)]
pub(super) struct ProcessStopHandles {
    worker: Option<platform::WorkerInterrupt>,
    pub(super) resolver: Option<crate::resolver::ResolverStopHandle>,
}

impl ProcessStopHandles {
    fn shutdown(&self, deadline: Instant) -> Result<(), String> {
        let resolver = self
            .resolver
            .as_ref()
            .map_or(Ok(()), |handle| handle.stop());
        let worker = self
            .worker
            .as_ref()
            .map_or(Ok(()), |handle| handle.shutdown(deadline));
        resolver.and(worker)
    }
}

impl Client {
    /// Replaces the current worker, optionally adding Python requirements first.
    pub(crate) async fn restart(
        &self,
        requirements: Vec<String>,
        grace: Duration,
    ) -> Result<Response, String> {
        let client = self.clone();
        let outcome =
            tokio::task::spawn_blocking(move || client.restart_blocking(requirements, grace))
                .await
                .map_err(|error| format!("worker restart task failed: {error}"))??;
        let finish = !matches!(&outcome, RestartOutcome::TerminalFailure(_));
        let response = match outcome {
            RestartOutcome::Restarted(mut output) => {
                output.push_line("[restarted]");
                output
            }
            RestartOutcome::Failed(failure) | RestartOutcome::TerminalFailure(failure) => {
                super::output::render_failure(String::new(), failure)
            }
        };
        if finish {
            self.finish_restart()?;
        }
        Ok(response)
    }

    fn restart_blocking(
        &self,
        requirements: Vec<String>,
        grace: Duration,
    ) -> Result<RestartOutcome, String> {
        let (stop_handles, deadline) = if requirements.is_empty() {
            self.begin_restart(grace)?
        } else {
            self.resolve_and_begin_restart(requirements, grace)?
        };
        if let Err(error) = stop_handles.shutdown(deadline) {
            self.fail_restart(deadline)?;
            return Ok(RestartOutcome::TerminalFailure(
                self.restart_failure(error)?,
            ));
        }
        let result = self.replace_worker();
        match result {
            Err(failure) => Ok(RestartOutcome::Failed(failure)),
            Ok(output) => Ok(RestartOutcome::Restarted(output)),
        }
    }

    fn resolve_and_begin_restart(
        &self,
        requirements: Vec<String>,
        grace: Duration,
    ) -> Result<(ProcessStopHandles, Instant), String> {
        let generation = self.admit()?;
        let environment = self.0.environment.as_ref().ok_or_else(|| {
            "Python requirements are unavailable with a custom worker".to_string()
        })?;
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        self.ensure_generation(&generation)?;
        let Some(candidate) = merge_python_requirements(environment.python.as_ref(), requirements)
        else {
            drop(environment);
            return self.begin_restart(grace);
        };

        let managed = match crate::resolver::resolve_python_host(candidate, |handle| {
            self.register_resolver_stop_handle(&generation, handle)
        }) {
            Ok(managed) => managed,
            Err(error) => {
                self.clear_resolver_stop_handle(&generation)?;
                return Err(error);
            }
        };

        let restart = self.begin_restart_after_resolution(&generation, grace)?;
        environment.python = Some(managed);
        Ok(restart)
    }

    fn replace_worker(&self) -> Result<Response, SendFailure> {
        let mut worker = self
            .0
            .worker
            .lock()
            .map_err(|_| SendFailure::from("worker lock poisoned".to_string()))?;
        self.ensure_restarting().map_err(SendFailure::from)?;
        let stopped = worker.finish_retirement().map_err(SendFailure::from)?;
        let evaluation_output = self
            .0
            .evaluation
            .lock()
            .map_err(|_| SendFailure::from("worker evaluation lock poisoned".to_string()))?
            .take()
            .map(|active| active.evaluation.take_restart_output())
            .transpose()?
            .unwrap_or_else(|| super::evaluation::RestartOutput::Output(Response::default()));
        let (evaluation_output, evaluation_stopped) = match evaluation_output {
            super::evaluation::RestartOutput::Output(output) => (output, false),
            super::evaluation::RestartOutput::WorkerStopped(output) => (output, true),
        };
        let mut output = Response::default();
        output.push_text(self.0.output.take());
        output.extend(evaluation_output);
        if stopped && !evaluation_stopped {
            output.push_line(super::output::WORKER_STOPPED_NOTICE);
        } else if matches!(&*worker, WorkerState::Initial) {
            *worker = WorkerState::Stopped;
        }

        if let Err(message) = self.start_worker(&mut worker, |stop_handle| {
            self.register_restart_stop_handle(stop_handle)
        }) {
            let message = match self.clear_restart_stop_handle() {
                Ok(()) => message,
                Err(clear_error) => format!(
                    "{message}; additionally failed to clear the worker interrupt: {clear_error}"
                ),
            };
            let startup = self.0.output.take();
            if !startup.is_empty() {
                output.push_line(startup);
            }
            return Err(SendFailure {
                output,
                message,
                worker_stopped: false,
            });
        }
        let startup = self.0.output.take();
        if !startup.is_empty() {
            output.push_line(startup);
        }
        Ok(output)
    }

    fn restart_failure(&self, message: String) -> Result<SendFailure, String> {
        let output = self
            .0
            .evaluation
            .lock()
            .map_err(|_| "worker evaluation lock poisoned".to_string())?
            .take()
            .map(|active| active.evaluation.take_restart_output())
            .transpose()?
            .map_or_else(Response::default, |output| match output {
                super::evaluation::RestartOutput::Output(output)
                | super::evaluation::RestartOutput::WorkerStopped(output) => output,
            });
        Ok(SendFailure {
            output,
            message,
            worker_stopped: false,
        })
    }

    pub(super) fn admit(&self) -> Result<WorkerGeneration, String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready => Ok(lifecycle.generation.clone()),
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    pub(super) fn generation_status(
        &self,
        expected: &WorkerGeneration,
    ) -> Result<GenerationStatus, String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        Ok(if !lifecycle.generation.is(expected) {
            GenerationStatus::Changed
        } else if lifecycle.state == LifecycleState::Ready {
            GenerationStatus::CurrentReady
        } else {
            GenerationStatus::CurrentClosing
        })
    }

    pub(super) fn ensure_generation(&self, expected: &WorkerGeneration) -> Result<(), String> {
        let generation = self.admit()?;
        if !generation.is(expected) {
            return Err("session restarted before the operation began".to_string());
        }
        Ok(())
    }

    fn ensure_restarting(&self) -> Result<(), String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Restarting { .. } => Ok(()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
            LifecycleState::Ready => Err("worker restart state changed".to_string()),
        }
    }

    fn begin_restart(&self, grace: Duration) -> Result<(ProcessStopHandles, Instant), String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Restarting { .. } => {
                return Err("worker is already restarting".to_string());
            }
            LifecycleState::ShuttingDown { .. } => {
                return Err("worker is shutting down".to_string());
            }
            LifecycleState::Ready
                if lifecycle.processes.worker.is_none()
                    && lifecycle.processes.resolver.is_some() =>
            {
                return Err("requirement preparation is still running".to_string());
            }
            LifecycleState::Ready => {}
        }
        let restart = lifecycle.start_restart(grace);
        drop(lifecycle);
        self.mark_evaluation_restarting()?;
        Ok(restart)
    }

    fn begin_restart_after_resolution(
        &self,
        expected: &WorkerGeneration,
        grace: Duration,
    ) -> Result<(ProcessStopHandles, Instant), String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.generation.is(expected) => {}
            LifecycleState::Ready => {
                return Err("session restarted before the operation began".to_string());
            }
            LifecycleState::Restarting { .. } => {
                return Err("worker is restarting".to_string());
            }
            LifecycleState::ShuttingDown { .. } => {
                return Err("worker is shutting down".to_string());
            }
        }
        lifecycle.processes.resolver = None;
        let restart = lifecycle.start_restart(grace);
        drop(lifecycle);
        self.mark_evaluation_restarting()?;
        Ok(restart)
    }

    fn mark_evaluation_restarting(&self) -> Result<(), String> {
        if let Some(active) = self
            .0
            .evaluation
            .lock()
            .map_err(|_| "worker evaluation lock poisoned".to_string())?
            .as_ref()
        {
            active.evaluation.begin_restart()?;
        }
        Ok(())
    }

    fn finish_restart(&self) -> Result<(), String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Restarting { .. } => {
                lifecycle.state = LifecycleState::Ready;
                Ok(())
            }
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
            LifecycleState::Ready => Err("worker restart state changed".to_string()),
        }
    }

    fn fail_restart(&self, deadline: Instant) -> Result<(), String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Restarting { .. } => {
                lifecycle.state = LifecycleState::ShuttingDown { deadline };
                Ok(())
            }
            LifecycleState::ShuttingDown { .. } => Ok(()),
            LifecycleState::Ready => Err("worker restart state changed".to_string()),
        }
    }

    pub(super) fn retire_failed_worker(
        &self,
        worker: &mut WorkerState,
        expected: &WorkerGeneration,
    ) -> Result<bool, String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        if lifecycle.state != LifecycleState::Ready || !lifecycle.generation.is(expected) {
            return Ok(false);
        }
        if !matches!(worker, WorkerState::Running(_)) {
            return Ok(false);
        }
        if let Err(error) = worker.retire(Instant::now()) {
            lifecycle.state = LifecycleState::ShuttingDown {
                deadline: Instant::now(),
            };
            return Err(error);
        }
        lifecycle.processes.worker = None;
        Ok(true)
    }

    pub(super) fn register_stop_handle(
        &self,
        expected: &WorkerGeneration,
        handle: platform::WorkerInterrupt,
    ) -> Result<(), String> {
        let (deadline, message) = {
            let mut lifecycle = self
                .0
                .lifecycle
                .lock()
                .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
            match lifecycle.state {
                LifecycleState::Ready if lifecycle.generation.is(expected) => {
                    lifecycle.processes.worker = Some(handle.clone());
                    return Ok(());
                }
                LifecycleState::Ready => (
                    Instant::now(),
                    "session restarted before the operation began",
                ),
                LifecycleState::Restarting { deadline } => (deadline, "worker is restarting"),
                LifecycleState::ShuttingDown { deadline } => (deadline, "worker is shutting down"),
            }
        };
        handle.shutdown(deadline)?;
        Err(message.to_string())
    }

    pub(super) fn clear_worker_stop_handle(
        &self,
        expected: &WorkerGeneration,
    ) -> Result<(), String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        if lifecycle.state == LifecycleState::Ready && lifecycle.generation.is(expected) {
            lifecycle.processes.worker = None;
        }
        Ok(())
    }

    fn register_restart_stop_handle(
        &self,
        handle: platform::WorkerInterrupt,
    ) -> Result<(), String> {
        let (deadline, message) = {
            let mut lifecycle = self
                .0
                .lifecycle
                .lock()
                .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
            match lifecycle.state {
                LifecycleState::Restarting { .. } => {
                    lifecycle.processes.worker = Some(handle.clone());
                    return Ok(());
                }
                LifecycleState::ShuttingDown { deadline } => (deadline, "worker is shutting down"),
                LifecycleState::Ready => (Instant::now(), "worker restart state changed"),
            }
        };
        handle.shutdown(deadline)?;
        Err(message.to_string())
    }

    fn clear_restart_stop_handle(&self) -> Result<(), String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        if matches!(lifecycle.state, LifecycleState::Restarting { .. }) {
            lifecycle.processes.worker = None;
        }
        Ok(())
    }

    pub(super) fn register_resolver_stop_handle(
        &self,
        expected: &WorkerGeneration,
        handle: crate::resolver::ResolverStopHandle,
    ) -> Result<(), String> {
        let message = {
            let mut lifecycle = self
                .0
                .lifecycle
                .lock()
                .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
            match lifecycle.state {
                LifecycleState::Ready if lifecycle.generation.is(expected) => {
                    lifecycle.processes.resolver = Some(handle.clone());
                    return Ok(());
                }
                LifecycleState::Ready => "session restarted before the operation began",
                LifecycleState::Restarting { .. } => "worker is restarting",
                LifecycleState::ShuttingDown { .. } => "worker is shutting down",
            }
        };
        handle.stop()?;
        Err(message.to_string())
    }

    pub(super) fn clear_resolver_stop_handle(
        &self,
        expected: &WorkerGeneration,
    ) -> Result<(), String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        if (lifecycle.state == LifecycleState::Ready && lifecycle.generation.is(expected))
            || matches!(lifecycle.state, LifecycleState::ShuttingDown { .. })
        {
            lifecycle.processes.resolver = None;
        }
        Ok(())
    }

    fn close_lifecycle(&self, deadline: Instant) -> Result<Option<ProcessStopHandles>, String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        if !matches!(lifecycle.state, LifecycleState::ShuttingDown { .. }) {
            lifecycle.state = LifecycleState::ShuttingDown { deadline };
        }
        let handles = std::mem::take(&mut lifecycle.processes);
        Ok((handles.worker.is_some() || handles.resolver.is_some()).then_some(handles))
    }

    /// Stops and reaps active worker and resolver process groups.
    pub(crate) async fn shutdown(&self, deadline: Instant) -> Result<(), String> {
        let Some(stop_handles) = self.close_lifecycle(deadline)? else {
            return Ok(());
        };
        let client = self.clone();
        tokio::task::spawn_blocking(move || {
            let resolver = stop_handles
                .resolver
                .map_or(Ok(()), |resolver| resolver.stop());
            let worker = stop_handles
                .worker
                .map_or(Ok(()), |worker| worker.shutdown(deadline));
            let stopped = resolver.and(worker);
            if stopped.is_ok() {
                let mut owner = client
                    .0
                    .worker
                    .lock()
                    .map_err(|_| "worker lock poisoned".to_string())?;
                owner.finish_retirement()?;
            }
            stopped
        })
        .await
        .map_err(|error| format!("process shutdown task failed: {error}"))?
    }
}
