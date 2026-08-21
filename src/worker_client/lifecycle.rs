use std::collections::BTreeSet;
use std::sync::Arc;
use std::time::{Duration, Instant};

use super::environment::{
    PythonEnvironment, ensure_python_additions_available, merge_python_requirements,
};
use super::evaluation::{RestartDelivery, RestartReservation};
use super::output::{Response, ResponseAcknowledgment, SendFailure};
use super::{Client, WorkerRetirement, WorkerRetirementFailure, WorkerState, platform};

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
    retiring_generation: Option<RetiringGeneration>,
    pub(super) requirement_changes: RequirementChangeState,
    pub(super) processes: ProcessStopHandles,
}

struct RetiringGeneration {
    generation: WorkerGeneration,
    disposition: OldGenerationCommitDisposition,
}

#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum OldGenerationCommitDisposition {
    Commit,
    DiscardForReplacement,
}

impl LifecycleControl {
    pub(super) fn new() -> Self {
        Self {
            state: LifecycleState::Ready,
            generation: WorkerGeneration::new(),
            retiring_generation: None,
            requirement_changes: RequirementChangeState::Available,
            processes: ProcessStopHandles::default(),
        }
    }

    fn start_restart(
        &mut self,
        grace: Duration,
        disposition: OldGenerationCommitDisposition,
    ) -> (ProcessStopHandles, Instant, WorkerGeneration) {
        let deadline = Instant::now() + grace;
        let stop_handles = self.processes.clone();
        self.retiring_generation = Some(RetiringGeneration {
            generation: self.generation.clone(),
            disposition,
        });
        self.state = LifecycleState::Restarting { deadline };
        self.generation = WorkerGeneration::new();
        self.processes.resolver = None;
        (stop_handles, deadline, self.generation.clone())
    }

    pub(super) fn old_generation_commit_disposition(
        &self,
        expected: &WorkerGeneration,
    ) -> Result<OldGenerationCommitDisposition, String> {
        match self.state {
            LifecycleState::Ready if self.generation.is(expected) => {
                Ok(OldGenerationCommitDisposition::Commit)
            }
            LifecycleState::Ready => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Restarting { .. } => self
                .retiring_generation
                .as_ref()
                .filter(|retiring| retiring.generation.is(expected))
                .map(|retiring| retiring.disposition)
                .ok_or_else(|| "worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
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
    Stopped(Option<super::WorkerProcessOutcome>),
    RestartOwnsWorker,
}

struct RestartContext {
    processes: ProcessStopHandles,
    deadline: Instant,
    generation: WorkerGeneration,
    evaluation: Option<RestartReservation>,
}

#[derive(Clone, Default)]
pub(super) struct ProcessStopHandles {
    worker: Option<platform::WorkerShutdownHandle>,
    pub(super) resolver: Option<crate::resolver::ResolverStopHandle>,
}

impl ProcessStopHandles {
    fn shutdown(&self, deadline: Instant) -> Result<(), String> {
        let mut errors = Vec::new();
        let mut worker_allowance = None;
        // Queue worker shutdown before resolver cancellation can release a
        // response command onto the retiring relay connection.
        if let Some(worker) = self.worker.as_ref() {
            let (allowance, requested) = worker.request_shutdown(deadline, deadline);
            worker_allowance = Some(allowance);
            if let Err(error) = requested {
                errors.push(error);
            }
        }
        if let Some(resolver) = self.resolver.as_ref()
            && let Err(error) = resolver.stop()
        {
            errors.push(error);
        }
        // The barrier lets the ordered consumer apply failures and finish a
        // cancelled resolver callback before relay retirement is enforced.
        if let (Some(worker), Some(allowance)) = (self.worker.as_ref(), worker_allowance)
            && let Err(error) = worker.finish_shutdown(deadline, allowance)
        {
            errors.push(error);
        }
        match errors.split_first() {
            None => Ok(()),
            Some((first, rest)) => Err(rest.iter().fold(first.clone(), |mut error, additional| {
                error.push_str("; additionally ");
                error.push_str(additional);
                error
            })),
        }
    }
}

impl Client {
    /// Sends SIGINT to the active resolver or live worker process.
    pub(crate) async fn interrupt(&self) -> Result<(), String> {
        let client = self.clone();
        tokio::task::spawn_blocking(move || client.interrupt_blocking())
            .await
            .map_err(|error| format!("worker interrupt task failed: {error}"))?
    }

    fn interrupt_blocking(&self) -> Result<(), String> {
        let processes = {
            let lifecycle = self
                .0
                .lifecycle
                .lock()
                .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
            lifecycle.processes.clone()
        };
        if let Some(resolver) = processes.resolver
            && resolver.interrupt()?
        {
            return Ok(());
        }
        processes
            .worker
            .ok_or_else(|| "worker is not running".to_string())?
            .interrupt()
    }

    /// Replaces the current worker, optionally adding requirements first.
    pub(crate) async fn restart(
        &self,
        requirements: super::Requirements,
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
        requirements: super::Requirements,
        grace: Duration,
    ) -> Result<Response, String> {
        let restart = if requirements.duckdb.is_empty()
            && requirements.python.is_empty()
            && requirements.r.is_empty()
        {
            self.begin_restart(grace)?
        } else {
            self.resolve_and_begin_restart(requirements, grace)?
        };
        if let Err(mut error) = restart.processes.shutdown(restart.deadline) {
            let retirement = self.finish_worker_retirement();
            let outcome = match retirement {
                Ok(WorkerRetirement::Stopped {
                    outcome,
                    failed: true,
                }) => outcome,
                Ok(
                    WorkerRetirement::Stopped { .. }
                    | WorkerRetirement::NeverStarted
                    | WorkerRetirement::AlreadyStopped,
                ) => None,
                Err(retirement_error) => {
                    error.push_str(&format!(
                        "; additionally failed to retire worker I/O: {retirement_error}"
                    ));
                    None
                }
            };
            self.fail_restart(restart.deadline)?;
            self.0
                .output
                .push_failure(SendFailure::from(error).worker_outcome(outcome));
            return Ok(self.0.output.take());
        }
        match self.replace_worker(restart.evaluation, restart.generation.clone()) {
            Ok(response) => {
                self.finish_restart(&restart.generation)?;
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
        requirements: super::Requirements,
        grace: Duration,
    ) -> Result<RestartContext, String> {
        let generation = self.admit()?;
        let environment = self
            .0
            .environment
            .as_ref()
            .ok_or_else(|| "managed requirements are unavailable".to_string())?;
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        self.ensure_generation(&generation)?;
        let super::Requirements { duckdb, python, r } = requirements;
        ensure_python_additions_available(&environment, &python)?;

        let duckdb_additions = duckdb.into_iter().collect::<BTreeSet<_>>();
        let duckdb_changed = !duckdb_additions.is_subset(&environment.duckdb_extensions);
        let duckdb_extensions = environment
            .duckdb_extensions
            .union(&duckdb_additions)
            .cloned()
            .collect::<BTreeSet<_>>();
        let python_candidate = merge_python_requirements(
            environment
                .python
                .as_ref()
                .and_then(PythonEnvironment::managed),
            python,
        );
        let mut r_additions = r.into_iter().collect::<BTreeSet<_>>();
        if environment.custom_worker {
            r_additions.extend(
                super::CUSTOM_DUCKDB_R_REQUIREMENTS
                    .iter()
                    .map(|requirement| (*requirement).to_string()),
            );
        }
        let current_r = environment
            .r
            .as_ref()
            .map(|managed| managed.requirements().iter().cloned().collect())
            .unwrap_or_default();
        let r_changed = !r_additions.is_subset(&current_r);
        if !duckdb_changed && python_candidate.is_none() && !r_changed {
            drop(environment);
            return self.begin_restart(grace);
        }

        let mut managed_r = environment.r.clone();
        if r_changed {
            let requirements = current_r.union(&r_additions).cloned().collect();
            let result = crate::resolver::resolve_r(requirements, |handle| {
                self.register_resolver_stop_handle(&generation, handle)
            });
            self.clear_resolver_stop_handle(&generation)?;
            managed_r = Some(result?);
        }

        if !duckdb_extensions.is_empty() && (duckdb_changed || r_changed) {
            let target = managed_r.as_ref().ok_or_else(|| {
                "DuckDB extension preparation requires a managed R environment".to_string()
            })?;
            let extensions = duckdb_extensions.iter().cloned().collect::<Vec<_>>();
            self.resolve_duckdb_extensions(&generation, std::slice::from_ref(target), &extensions)?;
        }

        let managed_python = if let Some(candidate) = python_candidate {
            let (_, resolver) = environment
                .python
                .as_ref()
                .ok_or_else(|| "managed Python environment is unavailable".to_string())?
                .managed_parts()?;
            let resolver = resolver.clone();
            let result = crate::resolver::resolve_python_host(
                candidate,
                &resolver,
                managed_r.as_ref(),
                |handle| self.register_resolver_stop_handle(&generation, handle),
            );
            self.clear_resolver_stop_handle(&generation)?;
            Some(result?)
        } else {
            None
        };

        self.commit_environment_and_begin_restart(
            &generation,
            grace,
            &mut environment,
            managed_python,
            managed_r,
            duckdb_extensions,
        )
    }

    fn commit_environment_and_begin_restart(
        &self,
        expected: &WorkerGeneration,
        grace: Duration,
        environment: &mut super::environment::Environment,
        managed_python: Option<crate::resolver::ManagedPython>,
        managed_r: Option<crate::resolver::ManagedR>,
        duckdb_extensions: BTreeSet<String>,
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
        if let Some(managed_python) = managed_python {
            environment
                .python
                .as_mut()
                .ok_or_else(|| "managed Python environment is unavailable".to_string())?
                .replace_managed(managed_python)?;
        }
        environment.r = managed_r;
        environment.duckdb_extensions = duckdb_extensions;
        let (processes, deadline, generation) =
            lifecycle.start_restart(grace, OldGenerationCommitDisposition::DiscardForReplacement);
        Ok(RestartContext {
            processes,
            deadline,
            generation,
            evaluation,
        })
    }

    /// Crosses the physical worker boundary before starting its replacement.
    ///
    /// Acquiring the worker waits for its active operation to end, and
    /// `finish_retirement()` joins its remaining I/O tasks. No old-worker output
    /// can be published after the stopped notice below.
    fn replace_worker(
        &self,
        evaluation: Option<RestartReservation>,
        generation: WorkerGeneration,
    ) -> Result<Response, String> {
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
        let retired_worker = matches!(retirement, WorkerRetirement::Stopped { .. });
        if let WorkerRetirement::Stopped {
            outcome: Some(outcome),
            failed: true,
        } = retirement
        {
            self.0.output.push_notice_line(outcome.diagnostic());
        }
        let (old_output, post_completion_output) = evaluation.as_ref().map_or_else(
            || (self.0.output.take(), Response::default()),
            |evaluation| evaluation.take_output(&self.0.output),
        );
        drop(worker);

        let mut response = Response::default();
        let mut wait_for_send = None;
        let mut interrupted_notice = None;
        if let Some(evaluation) = evaluation {
            let unfinished = evaluation.unfinished();
            if unfinished {
                interrupted_notice = Some(evaluation.active_stopped_notice());
            }
            if evaluation.waiting {
                let mut send_output = old_output;
                if unfinished {
                    send_output.push_notice(evaluation.stopped_notice());
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
        response.extend_at_boundary(post_completion_output);
        if let Some(notice) = interrupted_notice {
            response.push_notice(notice);
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

        let completion_generation = generation.clone();
        if let Err(mut failure) = self.start_worker(
            &mut worker,
            generation,
            false,
            |stop_handle| self.register_restart_stop_handle(stop_handle),
            || self.finish_restart(&completion_generation),
        ) {
            if let Err(clear_error) = self.clear_restart_stop_handle() {
                failure.message.push_str(&format!(
                    "; additionally failed to clear the worker shutdown handle: {clear_error}"
                ));
            }
            self.0.output.push_failure(failure);
            response.extend(self.0.output.take());
            return Ok(response);
        }
        response.extend(self.0.output.take());
        response.push_notice(super::output::WORKER_IDLE_NOTICE);
        Ok(response)
    }

    fn finish_worker_retirement(&self) -> Result<WorkerRetirement, String> {
        let mut worker = self
            .0
            .worker
            .lock()
            .map_err(|_| "worker lock poisoned".to_string())?;
        worker.finish_retirement()
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
        let (processes, deadline, generation) =
            lifecycle.start_restart(grace, OldGenerationCommitDisposition::Commit);
        Ok(RestartContext {
            processes,
            deadline,
            generation,
            evaluation,
        })
    }

    fn finish_restart(&self, expected: &WorkerGeneration) -> Result<(), String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        if !lifecycle.generation.is(expected) {
            return Ok(());
        }
        match lifecycle.state {
            LifecycleState::Restarting { .. } => {
                lifecycle.state = LifecycleState::Ready;
                lifecycle.retiring_generation = None;
                lifecycle.requirement_changes = RequirementChangeState::Available;
                Ok(())
            }
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
            LifecycleState::Ready => Ok(()),
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
                lifecycle.retiring_generation = None;
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
    ) -> Result<FailedWorkerStop, WorkerRetirementFailure> {
        let mut lifecycle = self.0.lifecycle.lock().map_err(|_| {
            WorkerRetirementFailure::from("worker lifecycle lock poisoned".to_string())
        })?;
        if lifecycle.state != LifecycleState::Ready || !lifecycle.generation.is(expected) {
            return Ok(FailedWorkerStop::RestartOwnsWorker);
        }
        if !matches!(worker, WorkerState::Running(_)) {
            return Err(WorkerRetirementFailure::from(
                "failed worker was not running".to_string(),
            ));
        }
        let outcome = match worker.stop_failed() {
            Ok(WorkerRetirement::Stopped { outcome, .. }) => outcome,
            Ok(WorkerRetirement::NeverStarted | WorkerRetirement::AlreadyStopped) => {
                unreachable!("a running failed worker should retire")
            }
            Err(error) => {
                lifecycle.state = LifecycleState::ShuttingDown {
                    deadline: Instant::now(),
                };
                return Err(error);
            }
        };
        lifecycle.processes.worker = None;
        Ok(FailedWorkerStop::Stopped(outcome))
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
            let stopped = stop_handles.shutdown(deadline);
            let retired = client.finish_worker_retirement().map(|_| ());
            match (stopped, retired) {
                (Ok(()), Ok(())) => Ok(()),
                (Err(error), Ok(())) | (Ok(()), Err(error)) => Err(error),
                (Err(error), Err(retirement_error)) => Err(format!(
                    "{error}; additionally failed to retire worker I/O: {retirement_error}"
                )),
            }
        })
        .await
        .map_err(|error| format!("process shutdown task failed: {error}"))?
    }
}
