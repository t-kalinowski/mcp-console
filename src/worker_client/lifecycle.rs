use std::sync::Arc;
use std::time::{Duration, Instant};

use super::environment::merge_python_requirements;
use super::evaluation::{RestartDelivery, RestartReservation};
use super::output::{Response, ResponseAcknowledgment, SendFailure};
use super::{Client, WorkerRetirement, WorkerState, platform};

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
    pub(super) requirement_changes: RequirementChangeState,
    pub(super) provisional_python: Option<crate::resolver::ManagedPython>,
    pub(super) processes: ProcessStopHandles,
}

impl LifecycleControl {
    pub(super) fn new() -> Self {
        Self {
            state: LifecycleState::Ready,
            generation: WorkerGeneration::new(),
            requirement_changes: RequirementChangeState::Available,
            provisional_python: None,
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

#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum RequirementChangeState {
    Available,
    RestartRequired,
}

pub(super) enum GenerationStatus {
    CurrentReady,
    CurrentClosing,
    Changed,
}

pub(super) enum FailedWorkerStop {
    Stopped,
    RestartOwnsWorker,
}

struct RestartContext {
    processes: ProcessStopHandles,
    deadline: Instant,
    evaluation: Option<RestartReservation>,
}

#[derive(Clone, Default)]
pub(super) struct ProcessStopHandles {
    worker: Option<platform::WorkerShutdownHandle>,
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
            .map_or(Ok(None), |handle| handle.shutdown(deadline).map(Some));
        let worker = worker.and_then(|shutdown| {
            shutdown.map_or(Ok(()), |shutdown| {
                shutdown
                    .join()
                    .map_err(|_| "worker shutdown sender task failed".to_string())
            })
        });
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
        let response =
            tokio::task::spawn_blocking(move || client.restart_blocking(requirements, grace))
                .await
                .map_err(|error| format!("worker restart task failed: {error}"))??;
        Ok(response)
    }

    fn restart_blocking(
        &self,
        requirements: Vec<String>,
        grace: Duration,
    ) -> Result<Response, String> {
        let restart = if requirements.is_empty() {
            self.begin_restart(grace)?
        } else {
            self.resolve_and_begin_restart(requirements, grace)?
        };
        if let Err(error) = restart.processes.shutdown(restart.deadline) {
            self.fail_restart(restart.deadline)?;
            self.0.output.push_failure(SendFailure::from(error));
            return Ok(self.0.output.take());
        }
        match self.replace_worker(restart.evaluation) {
            Ok(response) => {
                self.finish_restart()?;
                Ok(response)
            }
            Err(error) => {
                self.fail_restart(restart.deadline)?;
                self.0.output.push_failure(SendFailure::from(error));
                Ok(self.0.output.take())
            }
        }
    }

    fn resolve_and_begin_restart(
        &self,
        requirements: Vec<String>,
        grace: Duration,
    ) -> Result<RestartContext, String> {
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

        self.commit_environment_and_begin_restart(&generation, grace, &mut environment, managed)
    }

    /// Crosses the physical worker boundary before starting its replacement.
    ///
    /// Acquiring the worker waits for its sideband operation to end, and
    /// `finish_retirement()` joins its remaining I/O tasks. No old-worker output
    /// can be published after the stopped notice below.
    fn replace_worker(&self, evaluation: Option<RestartReservation>) -> Result<Response, String> {
        let mut worker = self
            .0
            .worker
            .lock()
            .map_err(|_| "worker lock poisoned".to_string())?;
        self.ensure_restarting()?;
        let retirement = worker.finish_retirement()?;
        if matches!(retirement, WorkerRetirement::NeverStarted) {
            *worker = WorkerState::Stopped;
        }
        let retired_worker = matches!(retirement, WorkerRetirement::Stopped);
        let old_output = self.0.output.take();
        drop(worker);

        let mut response = Response::default();
        let mut wait_for_send = None;
        let mut interrupted = false;
        if let Some(evaluation) = evaluation {
            let unfinished = evaluation.unfinished();
            interrupted = unfinished;
            let old_output = evaluation.project_response(old_output);
            if evaluation.waiting {
                let mut send_output = old_output;
                if unfinished {
                    send_output.push_notice(super::output::EVALUATION_STOPPED_BY_RESTART_NOTICE);
                    if retired_worker {
                        send_output.push_notice(super::output::WORKER_STOPPED_NOTICE);
                    }
                    send_output.mark_error();
                }
                match evaluation.deliver(send_output)? {
                    RestartDelivery::Waiting(acknowledged) => {
                        wait_for_send = Some(acknowledged);
                    }
                    RestartDelivery::Unclaimed(output) => response.extend(output),
                }
            } else {
                response.extend(old_output);
            }
        } else {
            response.extend(old_output);
        }
        if let Some(acknowledged) = wait_for_send
            && let Ok(ResponseAcknowledgment::Unclaimed(output)) = acknowledged.recv()
        {
            response.extend(output);
        }
        if interrupted {
            response.push_notice(super::output::ACTIVE_EVALUATION_STOPPED_NOTICE);
        }
        if retired_worker {
            response.push_notice(super::output::WORKER_STOPPED_NOTICE);
        }

        let mut worker = self
            .0
            .worker
            .lock()
            .map_err(|_| "worker lock poisoned".to_string())?;
        self.ensure_restarting()?;
        response.push_notice_line(super::output::WORKER_STARTING_NOTICE);

        if let Err(message) = self.start_worker(&mut worker, false, |stop_handle| {
            self.register_restart_stop_handle(stop_handle)
        }) {
            let message = match self.clear_restart_stop_handle() {
                Ok(()) => message,
                Err(clear_error) => format!(
                    "{message}; additionally failed to clear the worker shutdown handle: {clear_error}"
                ),
            };
            response.extend(self.0.output.take());
            response.push_server_failure(message);
            return Ok(response);
        }
        response.extend(self.0.output.take());
        response.push_notice(super::output::WORKER_IDLE_NOTICE);
        Ok(response)
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

    fn begin_restart(&self, grace: Duration) -> Result<RestartContext, String> {
        let mut evaluation = self.evaluation()?;
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
        let evaluation = evaluation
            .take()
            .map(|active| active.evaluation.reserve_for_restart())
            .transpose()?;
        let (processes, deadline) = lifecycle.start_restart(grace);
        Ok(RestartContext {
            processes,
            deadline,
            evaluation,
        })
    }

    fn commit_environment_and_begin_restart(
        &self,
        expected: &WorkerGeneration,
        grace: Duration,
        environment: &mut super::environment::Environment,
        managed: crate::resolver::ManagedPython,
    ) -> Result<RestartContext, String> {
        let mut evaluation = self.evaluation()?;
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
        let evaluation = evaluation
            .take()
            .map(|active| active.evaluation.reserve_for_restart())
            .transpose()?;
        environment.python = Some(managed);
        let (processes, deadline) = lifecycle.start_restart(grace);
        Ok(RestartContext {
            processes,
            deadline,
            evaluation,
        })
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
                lifecycle.requirement_changes = RequirementChangeState::Available;
                lifecycle.provisional_python = None;
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

    pub(super) fn stop_failed_worker(
        &self,
        worker: &mut WorkerState,
        expected: &WorkerGeneration,
    ) -> Result<FailedWorkerStop, String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        if lifecycle.state != LifecycleState::Ready || !lifecycle.generation.is(expected) {
            return Ok(FailedWorkerStop::RestartOwnsWorker);
        }
        if !matches!(worker, WorkerState::Running(_)) {
            return Err("failed worker was not running".to_string());
        }
        if let Err(error) = worker.stop(Instant::now()) {
            lifecycle.state = LifecycleState::ShuttingDown {
                deadline: Instant::now(),
            };
            return Err(error);
        }
        lifecycle.processes.worker = None;
        lifecycle.provisional_python = None;
        Ok(FailedWorkerStop::Stopped)
    }

    pub(super) fn register_stop_handle(
        &self,
        expected: &WorkerGeneration,
        handle: platform::WorkerShutdownHandle,
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
        handle
            .shutdown(deadline)?
            .join()
            .map_err(|_| "worker shutdown sender task failed".to_string())?;
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
        handle: platform::WorkerShutdownHandle,
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
        handle
            .shutdown(deadline)?
            .join()
            .map_err(|_| "worker shutdown sender task failed".to_string())?;
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
                .map_or(Ok(None), |worker| worker.shutdown(deadline).map(Some));
            let worker = worker.and_then(|shutdown| {
                shutdown.map_or(Ok(()), |shutdown| {
                    shutdown
                        .join()
                        .map_err(|_| "worker shutdown sender task failed".to_string())
                })
            });
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
