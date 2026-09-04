use std::sync::Arc;
use std::time::{Duration, Instant};

use super::environment::{Environment, RequirementDelta, ResolvedEnvironment};
use super::evaluation::{EvaluationReservation, RestartDelivery};
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
    controlled_send: Option<Arc<()>>,
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
            controlled_send: None,
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
    evaluation: Option<EvaluationReservation>,
}

/// Reserves lifecycle admission for one inline-control `send` call.
pub(super) struct ControlledSendAdmission {
    client: Client,
    token: Arc<()>,
    generation: WorkerGeneration,
}

impl ControlledSendAdmission {
    pub(super) fn generation(&self) -> WorkerGeneration {
        self.generation.clone()
    }
}

pub(super) struct RestartFailure {
    pub(super) message: String,
    pub(super) response: Response,
}

pub(super) struct RestartAttempt {
    pub(super) response: Response,
    pub(super) generation: Option<WorkerGeneration>,
}

struct WorkerReplacement {
    response: Response,
    ready: bool,
}

impl RestartFailure {
    fn new(message: String) -> Self {
        Self {
            message,
            response: Response::default(),
        }
    }

    fn with_response(message: String, response: Response) -> Self {
        Self { message, response }
    }
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

impl Drop for ControlledSendAdmission {
    fn drop(&mut self) {
        let Ok(mut lifecycle) = self.client.0.lifecycle.lock() else {
            return;
        };
        if lifecycle
            .controlled_send
            .as_ref()
            .is_some_and(|token| Arc::ptr_eq(token, &self.token))
        {
            lifecycle.controlled_send = None;
        }
    }
}

impl Client {
    pub(super) fn interrupt_standalone_blocking(&self) -> Result<(), String> {
        let resolver = {
            let lifecycle = self
                .0
                .lifecycle
                .lock()
                .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
            lifecycle.processes.resolver.clone()
        };
        if let Some(resolver) = resolver
            && resolver.interrupt()?
        {
            return Ok(());
        }

        let active = self.evaluation()?;
        let (processes, worker_allowed) = {
            let lifecycle = self
                .0
                .lifecycle
                .lock()
                .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
            let active_is_interruptible = active
                .as_ref()
                .map(|active| active.evaluation.is_interruptible())
                .transpose()?
                .unwrap_or(false);
            let worker_allowed = lifecycle.controlled_send.is_none()
                || (lifecycle.state == LifecycleState::Ready
                    && active_is_interruptible
                    && active
                        .as_ref()
                        .is_some_and(|active| active.generation.is(&lifecycle.generation)));
            (lifecycle.processes.clone(), worker_allowed)
        };
        if let Some(resolver) = processes.resolver
            && resolver.interrupt()?
        {
            return Ok(());
        }
        if worker_allowed {
            return processes
                .worker
                .ok_or_else(|| "worker is not running".to_string())?
                .interrupt();
        }
        Err("session control is in progress".to_string())
    }

    pub(super) fn interrupt_blocking(&self) -> Result<(), String> {
        let processes = {
            let lifecycle = self
                .0
                .lifecycle
                .lock()
                .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
            lifecycle.processes.clone()
        };
        Self::interrupt_processes(processes)
    }

    fn interrupt_processes(processes: ProcessStopHandles) -> Result<(), String> {
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

    /// Defers the replacement-ready marker when this admission owns a follow-up operation.
    pub(super) fn restart_blocking(
        &self,
        requirements: super::Requirements,
        grace: Duration,
        defer_idle: bool,
        control: Option<&ControlledSendAdmission>,
    ) -> Result<RestartAttempt, String> {
        let mut restart = if requirements.duckdb.is_empty()
            && requirements.python.is_empty()
            && requirements.r.is_empty()
        {
            self.begin_restart(grace, control)?
        } else {
            self.resolve_and_begin_restart(requirements, grace, control)?
        };
        if let Err(mut error) = restart.processes.shutdown(restart.deadline) {
            let retirement = self.finish_worker_retirement();
            let retired_worker = matches!(retirement, Ok(WorkerRetirement::Stopped { .. }));
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
            let mut response =
                match self.settle_reserved_evaluation(restart.evaluation.take(), retired_worker) {
                    Ok(response) => response,
                    Err(settlement) => {
                        error.push_str(&format!(
                            "; additionally failed to settle evaluation response ownership: {}",
                            settlement.message
                        ));
                        settlement.response
                    }
                };
            let transition = self.fail_restart(restart.deadline);
            self.0
                .output
                .push_failure(SendFailure::from(error).worker_outcome(outcome));
            response.extend_logical_region(self.0.output.take());
            return Ok(RestartAttempt {
                response: self.retain_transition_result(transition, response),
                generation: None,
            });
        }
        match self.replace_worker(
            &mut restart.evaluation,
            restart.generation.clone(),
            !defer_idle,
        ) {
            Ok(replacement) => {
                let transition = self.finish_restart(&restart.generation);
                let ready = replacement.ready && transition.is_ok();
                Ok(RestartAttempt {
                    response: self.retain_transition_result(transition, replacement.response),
                    generation: ready.then_some(restart.generation),
                })
            }
            Err(mut failure) => {
                if restart.evaluation.is_some() {
                    match self.settle_reserved_evaluation(restart.evaluation.take(), false) {
                        Ok(response) => failure.response.extend(response),
                        Err(settlement) => {
                            failure.message.push_str(&format!(
                                "; additionally failed to settle evaluation response ownership: {}",
                                settlement.message
                            ));
                            failure.response.extend(settlement.response);
                        }
                    }
                }
                let transition = self.fail_restart(restart.deadline);
                self.0
                    .output
                    .push_failure(SendFailure::from(failure.message));
                failure.response.extend(self.0.output.take());
                Ok(RestartAttempt {
                    response: self.retain_transition_result(transition, failure.response),
                    generation: None,
                })
            }
        }
    }

    fn resolve_and_begin_restart(
        &self,
        requirements: super::Requirements,
        grace: Duration,
        control: Option<&ControlledSendAdmission>,
    ) -> Result<RestartContext, String> {
        let generation = match control {
            Some(control) => control.generation(),
            None => self.admit()?,
        };
        let environment = self
            .0
            .environment
            .as_ref()
            .ok_or_else(|| "managed requirements are unavailable".to_string())?;
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        self.ensure_generation(&generation)?;
        let delta = RequirementDelta::calculate(&environment, requirements)?;
        if delta.is_empty() {
            drop(environment);
            return self.begin_restart(grace, control);
        }
        let resolved = self
            .resolve_prestart_environment(&generation, &environment, delta)
            .map_err(|failure| failure.into_message())?;

        self.commit_environment_and_begin_restart(
            &generation,
            grace,
            &mut environment,
            resolved,
            control,
        )
    }

    fn commit_environment_and_begin_restart(
        &self,
        expected: &WorkerGeneration,
        grace: Duration,
        environment: &mut Environment,
        resolved: ResolvedEnvironment,
        control: Option<&ControlledSendAdmission>,
    ) -> Result<RestartContext, String> {
        let mut evaluation = self.evaluation()?;
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        let owns_control = match control {
            Some(control) => lifecycle
                .controlled_send
                .as_ref()
                .is_some_and(|token| Arc::ptr_eq(token, &control.token)),
            None => lifecycle.controlled_send.is_none(),
        };
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.generation.is(expected) && owns_control => {}
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
        if let Some(managed_python) = resolved.managed_python {
            environment
                .python
                .as_mut()
                .ok_or_else(|| "managed Python environment is unavailable".to_string())?
                .replace_managed(managed_python)?;
        }
        environment.r = resolved.managed_r;
        environment.duckdb_extensions = resolved.duckdb_extensions;
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
        evaluation: &mut Option<EvaluationReservation>,
        generation: WorkerGeneration,
        report_idle: bool,
    ) -> Result<WorkerReplacement, RestartFailure> {
        let mut worker = self
            .0
            .worker
            .lock()
            .map_err(|_| RestartFailure::new("worker lock poisoned".to_string()))?;
        self.ensure_restarting().map_err(RestartFailure::new)?;
        let retirement = worker.finish_retirement().map_err(RestartFailure::new)?;
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
        drop(worker);

        let mut response = self.settle_reserved_evaluation(evaluation.take(), retired_worker)?;

        let mut worker = match self.0.worker.lock() {
            Ok(worker) => worker,
            Err(_) => {
                return Err(RestartFailure::with_response(
                    "worker lock poisoned".to_string(),
                    response,
                ));
            }
        };
        if let Err(error) = self.ensure_restarting() {
            return Err(RestartFailure::with_response(error, response));
        }
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
            return Ok(WorkerReplacement {
                response,
                ready: false,
            });
        }
        response.extend(self.0.output.take());
        if report_idle {
            response.push_notice(super::output::WORKER_IDLE_NOTICE);
        }
        Ok(WorkerReplacement {
            response,
            ready: true,
        })
    }

    /// Settles one reserved evaluation before restart publishes its own response.
    pub(super) fn settle_reserved_evaluation(
        &self,
        mut evaluation: Option<EvaluationReservation>,
        retired_worker: bool,
    ) -> Result<Response, RestartFailure> {
        let (old_output, post_completion_output) = evaluation.as_mut().map_or_else(
            || (self.0.output.take(), Response::default()),
            |evaluation| evaluation.take_output(&self.0.output),
        );

        let mut response = Response::default();
        let mut wait_for_send = None;
        let mut output_after_delivery = Response::default();
        let mut waiting_response_includes_stopped = false;
        let mut reclaimed_worker_stopped = false;
        let mut interrupted_notice = None;
        let mut settlement_error = None;
        if let Some(mut evaluation) = evaluation {
            let unfinished = evaluation.unfinished();
            if unfinished {
                interrupted_notice = Some(evaluation.active_stopped_notice());
            }
            if let Some(delivery) = evaluation.take_pending_delivery() {
                wait_for_send = Some(delivery);
                output_after_delivery = old_output;
            } else if evaluation.waiting {
                let mut send_output = old_output;
                if unfinished {
                    send_output.push_notice(evaluation.stopped_notice());
                    if retired_worker {
                        send_output.push_notice(super::output::WORKER_STOPPED_NOTICE);
                        waiting_response_includes_stopped = true;
                    }
                    send_output.mark_error();
                }
                match evaluation.deliver(send_output) {
                    Ok(RestartDelivery::Waiting(acknowledged)) => {
                        wait_for_send = Some(acknowledged);
                    }
                    Ok(RestartDelivery::Unclaimed(output)) => {
                        response.extend(output);
                        reclaimed_worker_stopped = waiting_response_includes_stopped;
                    }
                    Err(failure) => {
                        response.extend(failure.response);
                        reclaimed_worker_stopped = waiting_response_includes_stopped;
                        settlement_error = Some(failure.message);
                    }
                }
            } else {
                response.extend(old_output);
            }
        } else {
            response.extend(old_output);
        }
        if let Some(acknowledged) = wait_for_send {
            match acknowledged.recv().expect(
                "a response delivery sender must return its owned response before disconnecting",
            ) {
                ResponseAcknowledgment::Delivered => {}
                ResponseAcknowledgment::Unclaimed(output) => {
                    response.extend(output);
                    reclaimed_worker_stopped = waiting_response_includes_stopped;
                }
            }
        }
        response.extend_logical_region(output_after_delivery);
        response.extend_logical_region(post_completion_output);
        if let Some(notice) = interrupted_notice {
            response.push_notice(notice);
        }
        if retired_worker && !reclaimed_worker_stopped {
            response.push_notice(super::output::WORKER_STOPPED_NOTICE);
        }
        match settlement_error {
            Some(error) => Err(RestartFailure::with_response(error, response)),
            None => Ok(response),
        }
    }

    fn retain_transition_result(
        &self,
        transition: Result<(), String>,
        mut response: Response,
    ) -> Response {
        if let Err(error) = transition {
            self.0.output.push_failure(SendFailure::from(error));
            response.extend(self.0.output.take());
        }
        response
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
            LifecycleState::Ready if lifecycle.controlled_send.is_none() => {
                Ok(lifecycle.generation.clone())
            }
            LifecycleState::Ready => Err("session control is in progress".to_string()),
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    pub(super) fn begin_controlled_send(&self) -> Result<ControlledSendAdmission, String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.controlled_send.is_none() => {}
            LifecycleState::Ready => {
                return Err("session control is already in progress".to_string());
            }
            LifecycleState::Restarting { .. } => {
                return Err("worker is restarting".to_string());
            }
            LifecycleState::ShuttingDown { .. } => {
                return Err("worker is shutting down".to_string());
            }
        }
        let token = Arc::new(());
        lifecycle.controlled_send = Some(token.clone());
        Ok(ControlledSendAdmission {
            client: self.clone(),
            token,
            generation: lifecycle.generation.clone(),
        })
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
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.generation.is(expected) => Ok(()),
            LifecycleState::Ready => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    pub(super) fn ensure_ordinary_generation(
        &self,
        expected: &WorkerGeneration,
    ) -> Result<(), String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready
                if lifecycle.generation.is(expected) && lifecycle.controlled_send.is_none() =>
            {
                Ok(())
            }
            LifecycleState::Ready if !lifecycle.generation.is(expected) => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Ready => Err("session control is in progress".to_string()),
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    pub(super) fn ensure_controlled_generation(
        &self,
        admission: &ControlledSendAdmission,
        expected: &WorkerGeneration,
    ) -> Result<(), String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        let owns_control = lifecycle
            .controlled_send
            .as_ref()
            .is_some_and(|token| Arc::ptr_eq(token, &admission.token));
        match lifecycle.state {
            LifecycleState::Ready if owns_control && lifecycle.generation.is(expected) => Ok(()),
            LifecycleState::Ready if !lifecycle.generation.is(expected) => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Ready => Err("session control admission changed".to_string()),
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
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

    fn begin_restart(
        &self,
        grace: Duration,
        control: Option<&ControlledSendAdmission>,
    ) -> Result<RestartContext, String> {
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
        let owns_control = match control {
            Some(control) => lifecycle
                .controlled_send
                .as_ref()
                .is_some_and(|token| Arc::ptr_eq(token, &control.token)),
            None => lifecycle.controlled_send.is_none(),
        };
        if !owns_control {
            return Err("session control is in progress".to_string());
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::worker_client::RResolver;
    use crate::worker_client::evaluation::EvaluationWait;
    use crate::worker_client::output::{Content, SendResponse, render_response};
    use crate::worker_protocol::ConsoleChannel;

    #[tokio::test]
    async fn replacement_failure_preserves_an_unclaimed_assembled_response() {
        let client = Client::with_arguments(
            std::path::PathBuf::from("unused-worker"),
            Vec::new(),
            None,
            None,
            RResolver::Discover,
        );
        let evaluation = Arc::new(super::super::Evaluation::new(
            crate::transcript::Transcript::new(true),
            None,
            client.0.output.clone(),
            Response::default(),
            Response::default(),
            false,
        ));
        evaluation.complete_cell(Ok(()));
        let claim = evaluation.claim().unwrap();
        let EvaluationWait::Completed(response) =
            evaluation.wait(claim, Duration::ZERO).await.unwrap()
        else {
            panic!("completed evaluation did not assemble its response")
        };
        let response = render_response(SendResponse::Completed(response));
        let mut reservation = Some(evaluation.reserve_for_restart().unwrap());
        let failure = match client.replace_worker(&mut reservation, WorkerGeneration::new(), true) {
            Ok(_) => panic!("replacement unexpectedly succeeded outside a restart"),
            Err(failure) => failure,
        };
        assert_eq!(failure.message, "worker restart state changed");
        assert!(reservation.is_some());
        let (_, _, delivery) = response.into_parts();
        delivery.unwrap().unclaimed();

        let recovered = client
            .settle_reserved_evaluation(reservation.take(), false)
            .unwrap_or_else(|failure| panic!("{}", failure.message));
        let (content, is_error, delivery) = recovered.into_parts();

        assert!(!is_error);
        assert!(delivery.is_none());
        assert!(matches!(content.as_slice(), [Content::Text(text)] if text == "[done]"));
        let (remaining, is_error, delivery) = client.0.output.take().into_parts();
        assert!(remaining.is_empty());
        assert!(!is_error);
        assert!(delivery.is_none());
    }

    #[tokio::test]
    async fn failed_restart_excludes_a_delivered_response_but_keeps_later_output() {
        let client = Client::with_arguments(
            std::path::PathBuf::from("unused-worker"),
            Vec::new(),
            None,
            None,
            RResolver::Discover,
        );
        let evaluation = Arc::new(super::super::Evaluation::new(
            crate::transcript::Transcript::new(true),
            None,
            client.0.output.clone(),
            Response::default(),
            Response::default(),
            false,
        ));
        evaluation.complete_cell(Ok(()));
        let claim = evaluation.claim().unwrap();
        let EvaluationWait::Completed(response) =
            evaluation.wait(claim, Duration::ZERO).await.unwrap()
        else {
            panic!("completed evaluation did not assemble its response")
        };
        let response = render_response(SendResponse::Completed(response));
        client
            .0
            .output
            .push_console_text(ConsoleChannel::Output, "later output".to_string());
        let reservation = evaluation.reserve_for_restart().unwrap();
        let (_, _, delivery) = response.into_parts();
        delivery.unwrap().delivered();

        let recovered = client
            .settle_reserved_evaluation(Some(reservation), false)
            .unwrap_or_else(|failure| panic!("{}", failure.message));
        let (content, is_error, delivery) = recovered.into_parts();

        assert!(!is_error);
        assert!(delivery.is_none());
        assert!(matches!(content.as_slice(), [Content::Text(text)] if text == "later output"));
        let (remaining, is_error, delivery) = client.0.output.take().into_parts();
        assert!(remaining.is_empty());
        assert!(!is_error);
        assert!(delivery.is_none());
    }
}
