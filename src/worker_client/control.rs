use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use super::environment::{PreparationIntent, PrepareResult};
use super::evaluation::{self, Evaluation, EvaluationWait};
use super::lifecycle::{ControlledSendAdmission, WorkerGeneration};
use super::output::{self, Response, SendFailure, SendResponse};
use super::send::send_response_from_wait;
use super::{
    Client, INTERRUPT_GRACE, Requirements, SendControl, SendRequest, WORKER_SHUTDOWN_GRACE,
};

enum ControlledEvaluation {
    Started(Arc<Evaluation>, evaluation::WaitClaim),
    Returned(Response),
    Observe {
        evaluation: Arc<Evaluation>,
        wait_claim: evaluation::WaitClaim,
        cell_not_run: bool,
    },
}

enum PriorEvaluation {
    None,
    Completed(Response),
    Active(Arc<Evaluation>),
    Failed(Response),
}

enum ControlledStdinFailure {
    ActiveEvaluation(String),
    IdleWorker(SendFailure),
}

fn interrupted_cell_not_run_response(wait: EvaluationWait) -> Response {
    let mut response = send_response_from_wait(wait);
    match &mut response {
        SendResponse::Idle(output)
        | SendResponse::Failed(output)
        | SendResponse::Running(output)
        | SendResponse::InputRequested(output)
        | SendResponse::Completed(output)
        | SendResponse::ReplacementStarting(output)
        | SendResponse::ReplacementReady(output)
        | SendResponse::Restarted(output) => {
            output.push_tool_error("interrupted evaluation is still active; cell was not run");
        }
    }
    output::render_response(response)
}

impl Client {
    pub(super) async fn send_controlled(
        &self,
        control: SendControl,
        request: SendRequest,
    ) -> Result<Response, String> {
        let deadline = request.deadline;
        let direct_restart_error = matches!(control, SendControl::Restart)
            && request.requirements.is_some()
            && request.cell.is_none();
        let client = self.clone();
        let admission = tokio::task::spawn_blocking(move || {
            client.control_and_start_evaluation(control, request)
        })
        .await;
        let admission = match admission {
            Ok(Ok(admission)) => admission,
            Ok(Err(error)) if direct_restart_error => return Err(error),
            Ok(Err(error)) => return Ok(output::direct_failure(error)),
            Err(error) => {
                return Ok(output::direct_failure(format!(
                    "session control task failed: {error}"
                )));
            }
        };
        Ok(match admission {
            ControlledEvaluation::Started(evaluation, wait_claim) => {
                match evaluation
                    .wait(
                        wait_claim,
                        deadline.saturating_duration_since(Instant::now()),
                    )
                    .await
                {
                    Ok(wait) => output::render_response(send_response_from_wait(wait)),
                    Err(error) => output::direct_failure(error),
                }
            }
            ControlledEvaluation::Returned(response) => response,
            ControlledEvaluation::Observe {
                evaluation,
                wait_claim,
                cell_not_run,
            } => match evaluation
                .wait(
                    wait_claim,
                    if cell_not_run {
                        Duration::ZERO
                    } else {
                        deadline.saturating_duration_since(Instant::now())
                    },
                )
                .await
            {
                Ok(wait) if cell_not_run => interrupted_cell_not_run_response(wait),
                Ok(wait) => output::render_response(send_response_from_wait(wait)),
                Err(error) => output::direct_failure(error),
            },
        })
    }

    fn control_and_start_evaluation(
        &self,
        requested: SendControl,
        request: SendRequest,
    ) -> Result<ControlledEvaluation, String> {
        let SendRequest {
            cell,
            stdin,
            requirements,
            control: _,
            deadline: _,
            transcript,
            call_id,
        } = request;
        let standalone_interrupt = matches!(requested, SendControl::Interrupt)
            && cell.is_none()
            && requirements.is_none()
            && stdin.as_ref().is_none_or(String::is_empty);
        let control = match self.begin_controlled_send() {
            Ok(control) => control,
            Err(_) if standalone_interrupt => {
                // A controlled restart owns admission while its resolver is live.
                // Preserve resolver-first signaling for an empty interrupt.
                self.interrupt_standalone_blocking()?;
                std::thread::sleep(INTERRUPT_GRACE);
                let control = match self.begin_controlled_send() {
                    Ok(control) => control,
                    Err(_) => {
                        // The interrupted control retains output recovery ownership.
                        return Ok(ControlledEvaluation::Returned(output::render_response(
                            SendResponse::Running(Response::default()),
                        )));
                    }
                };
                let generation = control.generation();
                return self.continue_after_interrupt(
                    &control,
                    generation,
                    cell,
                    requirements,
                    transcript,
                    call_id,
                );
            }
            Err(error) => return Err(error),
        };
        match requested {
            SendControl::Interrupt => self.interrupt_and_start_evaluation(
                &control,
                cell,
                stdin,
                requirements,
                transcript,
                call_id,
            ),
            SendControl::Restart => self.restart_and_start_evaluation(
                &control,
                cell,
                stdin,
                requirements,
                transcript,
                call_id,
            ),
        }
    }

    fn return_controlled_response(&self, mut response: Response) -> ControlledEvaluation {
        response.recover_to(self.0.output.clone());
        ControlledEvaluation::Returned(response)
    }

    fn return_controlled_failure(
        &self,
        mut response: Response,
        failure: SendFailure,
    ) -> ControlledEvaluation {
        response.extend_logical_region(self.0.output.take());
        response.push_failure(failure);
        self.return_controlled_response(response)
    }

    fn observe_interrupted_evaluation(
        &self,
        evaluation: Arc<Evaluation>,
        cell_not_run: bool,
    ) -> ControlledEvaluation {
        match evaluation.claim() {
            Ok(wait_claim) => ControlledEvaluation::Observe {
                evaluation,
                wait_claim,
                cell_not_run,
            },
            Err(error) => {
                let mut response = Response::default();
                if cell_not_run {
                    response.push_tool_error(
                        "interrupted evaluation is still active; cell was not run",
                    );
                } else {
                    response.push_tool_error(error);
                }
                self.return_controlled_response(response)
            }
        }
    }

    fn interrupt_and_start_evaluation(
        &self,
        control: &ControlledSendAdmission,
        cell: Option<crate::cell::Cell>,
        stdin: Option<String>,
        requirements: Option<Requirements>,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<ControlledEvaluation, String> {
        let generation = control.generation();
        if self.python_preparation() && requirements.is_some() {
            return Err("Python requirements cannot accompany control: interrupt; prepare before first use or with control: restart".into());
        }
        self.interrupt_blocking()?;
        self.ensure_controlled_generation(control, &generation)?;
        match self.submit_controlled_stdin(stdin, &generation, control) {
            Ok(()) => {}
            Err(ControlledStdinFailure::ActiveEvaluation(error)) => return Err(error),
            Err(ControlledStdinFailure::IdleWorker(failure)) => {
                return Ok(self.return_controlled_failure(Response::default(), failure));
            }
        }
        std::thread::sleep(INTERRUPT_GRACE);
        self.continue_after_interrupt(control, generation, cell, requirements, transcript, call_id)
    }

    fn continue_after_interrupt(
        &self,
        control: &ControlledSendAdmission,
        generation: WorkerGeneration,
        cell: Option<crate::cell::Cell>,
        requirements: Option<Requirements>,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<ControlledEvaluation, String> {
        self.ensure_controlled_generation(control, &generation)?;

        // An interrupted preparation retains read admission until its resolver
        // settles. A code-free interrupt must still answer after the grace.
        let _operation = if cell.is_none() {
            match self.0.admission.try_write() {
                Ok(operation) => operation,
                Err(_) => {
                    // The preparation retains output recovery ownership.
                    return Ok(ControlledEvaluation::Returned(output::render_response(
                        SendResponse::Running(Response::default()),
                    )));
                }
            }
        } else {
            self.admit_controlled_operation()
        };
        let prior = self.prior_evaluation_after_interrupt(&generation, control, cell.is_some())?;
        if cell.is_none() {
            return match prior {
                PriorEvaluation::Active(evaluation) => {
                    Ok(self.observe_interrupted_evaluation(evaluation, false))
                }
                PriorEvaluation::Completed(response) => {
                    Ok(self.return_controlled_response(response))
                }
                PriorEvaluation::Failed(response) => Ok(self.return_controlled_response(response)),
                PriorEvaluation::None => match self.take_idle_response(&generation) {
                    Ok(response) => {
                        Ok(self.return_controlled_response(output::render_response(response)))
                    }
                    Err(failure) => {
                        Ok(self.return_controlled_failure(Response::default(), failure))
                    }
                },
            };
        }
        let mut control_prelude = match prior {
            PriorEvaluation::Active(evaluation) => {
                return Ok(self.observe_interrupted_evaluation(evaluation, true));
            }
            PriorEvaluation::Completed(response) => response,
            PriorEvaluation::None => Response::default(),
            PriorEvaluation::Failed(mut response) => {
                response.push_tool_error(
                    "interrupted evaluation could not be settled; cell was not run",
                );
                return Ok(self.return_controlled_response(response));
            }
        };
        if let Some(requirements) = requirements {
            if let Err(error) = requirements.validate() {
                control_prelude.push_tool_error(error);
                return Ok(self.return_controlled_response(control_prelude));
            }
            let preparation = match self.admit_preparation() {
                Ok(preparation) => preparation,
                Err(error) => {
                    control_prelude.push_tool_error(error);
                    return Ok(self.return_controlled_response(control_prelude));
                }
            };
            let prepared = self.prepare_admitted(
                requirements,
                &generation,
                &preparation,
                PreparationIntent::BeforeEvaluation,
            );
            drop(preparation);
            match prepared {
                Err(error) => {
                    control_prelude.push_tool_error(error);
                    return Ok(self.return_controlled_response(control_prelude));
                }
                Ok(PrepareResult::Prepared) => {}
                Ok(PrepareResult::RestartRequired) => {
                    control_prelude
                        .push_tool_error("requirements require session restart; cell was not run");
                    return Ok(self.return_controlled_response(control_prelude));
                }
                Ok(PrepareResult::Failed(response) | PrepareResult::WorkerStopped(response)) => {
                    control_prelude.extend_logical_region(response);
                    return Ok(self.return_controlled_response(control_prelude));
                }
            }
        }
        self.ensure_controlled_generation(control, &generation)?;
        let mut prelude = Some(control_prelude);
        let (evaluation, wait_claim) = match self.start_evaluation_admitted(
            cell.expect("controlled cell presence was checked"),
            None,
            generation,
            transcript,
            call_id,
            Some(control),
            &mut prelude,
            None,
        ) {
            Ok(started) => started,
            Err(error) => {
                let mut response = prelude.take().unwrap_or_default();
                response.push_tool_error(format!("{error}; cell was not run"));
                return Ok(self.return_controlled_response(response));
            }
        };
        Ok(ControlledEvaluation::Started(evaluation, wait_claim))
    }

    fn restart_and_start_evaluation(
        &self,
        control: &ControlledSendAdmission,
        cell: Option<crate::cell::Cell>,
        stdin: Option<String>,
        requirements: Option<Requirements>,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<ControlledEvaluation, String> {
        let requirements = requirements.unwrap_or_default();
        let stdin_follows = stdin.as_ref().is_some_and(|stdin| !stdin.is_empty());
        let restart = self.restart_blocking(
            requirements,
            WORKER_SHUTDOWN_GRACE,
            cell.is_some() || stdin_follows,
            Some(control),
        )?;
        let _operation = self.admit_controlled_operation();
        let Some(generation) = restart.generation else {
            let mut response = restart.response;
            if cell.is_some() {
                response.push_tool_error("session restart did not complete; cell was not run");
            }
            return Ok(self.return_controlled_response(response));
        };
        self.0.startup_failed.store(false, Ordering::Release);
        self.finish_startup(Ok(()));
        self.ensure_controlled_generation(control, &generation)?;
        let Some(cell) = cell else {
            let response = restart.response;
            if let Some(stdin) = stdin.filter(|stdin| !stdin.is_empty()) {
                if let Err(failure) = self.write_idle_stdin_blocking(stdin, generation.clone()) {
                    return Ok(self.return_controlled_failure(response, failure));
                }
                return Ok(self.return_controlled_response(output::render_response(
                    SendResponse::ReplacementReady(response),
                )));
            }
            return Ok(self.return_controlled_response(response));
        };
        let mut prelude = Some(restart.response);
        let (evaluation, wait_claim) = match self.start_evaluation_admitted(
            cell,
            stdin,
            generation,
            transcript,
            call_id,
            Some(control),
            &mut prelude,
            None,
        ) {
            Ok(started) => started,
            Err(error) => {
                let mut response = prelude.take().unwrap_or_default();
                response.push_tool_error(format!("{error}; cell was not run"));
                return Ok(self.return_controlled_response(response));
            }
        };
        Ok(ControlledEvaluation::Started(evaluation, wait_claim))
    }

    fn submit_controlled_stdin(
        &self,
        stdin: Option<String>,
        generation: &WorkerGeneration,
        control: &ControlledSendAdmission,
    ) -> Result<(), ControlledStdinFailure> {
        let Some(stdin) = stdin.filter(|stdin| !stdin.is_empty()) else {
            return Ok(());
        };
        self.ensure_controlled_generation(control, generation)
            .map_err(ControlledStdinFailure::ActiveEvaluation)?;
        if let Some(active) = self
            .current_evaluation()
            .map_err(ControlledStdinFailure::ActiveEvaluation)?
        {
            if !active.generation.is(generation) {
                return Err(ControlledStdinFailure::ActiveEvaluation(
                    "session restarted before stdin was queued".to_string(),
                ));
            }
            active
                .evaluation
                .submit_stdin(stdin)
                .map_err(ControlledStdinFailure::ActiveEvaluation)
        } else {
            self.write_idle_stdin_blocking(stdin, generation.clone())
                .map_err(ControlledStdinFailure::IdleWorker)
        }
    }

    fn prior_evaluation_after_interrupt(
        &self,
        generation: &WorkerGeneration,
        control: &ControlledSendAdmission,
        cell_follows: bool,
    ) -> Result<PriorEvaluation, String> {
        let mut active = self.evaluation()?;
        let Some(current) = active.as_ref() else {
            return Ok(PriorEvaluation::None);
        };
        self.ensure_controlled_generation(control, generation)?;
        if !current.generation.is(generation) {
            return Err("session restarted before the interrupted evaluation settled".to_string());
        }
        let evaluation = current.evaluation.clone();
        let reservation = if cell_follows {
            evaluation.reserve_completed_for_handoff()?
        } else {
            evaluation.reserve_completed_for_delivery()?
        };
        let Some(reservation) = reservation else {
            return Ok(PriorEvaluation::Active(evaluation));
        };
        *active = None;
        drop(active);
        match self.settle_reserved_evaluation(Some(reservation), false) {
            Ok(response) => Ok(PriorEvaluation::Completed(response)),
            Err(mut failure) => {
                failure.response.push_tool_error(failure.message);
                Ok(PriorEvaluation::Failed(failure.response))
            }
        }
    }
}
