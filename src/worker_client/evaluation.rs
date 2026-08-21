use std::sync::{Arc, Mutex, mpsc};
use std::time::{Duration, Instant};

use super::output::{
    OutputCheckpoint, OutputTape, Response, ResponseAcknowledgment, SendFailure, project_completed,
    project_replacement_ready,
};

const INPUT_REQUEST_GRACE: Duration = Duration::from_millis(10);
const CELL_COMPLETION_GRACE: Duration = Duration::from_millis(1);

pub(super) struct Evaluation {
    state: Mutex<EvaluationState>,
    changed: tokio::sync::Notify,
    transcript: crate::transcript::Transcript,
    call_id: Option<u64>,
    output: OutputTape,
    runtime: tokio::runtime::Handle,
}

struct EvaluationState {
    phase: EvaluationPhase,
    completion_checkpoint: Option<OutputCheckpoint>,
    /// Whether a waiter already drained the response for a completion phase.
    completion_collected: bool,
    input_report_at: Option<Instant>,
    /// Whether one `send` currently owns the right to drain this evaluation's response.
    waiting: bool,
    restart_reserved: bool,
    restart_handoff: Option<Response>,
    #[cfg(target_os = "macos")]
    stdin: Option<super::platform::StdinSender>,
    pending_stdin: String,
}

#[derive(Clone, Copy)]
enum EvaluationPhase {
    Evaluating,
    CellCompletionGrace(Instant),
    ReplacementStarting,
    Complete(CompletionKind),
}

#[derive(Clone, Copy)]
enum CompletionKind {
    Cell,
    ReplacementReady,
    ReplacementFailed,
}

pub(super) enum EvaluationWait {
    Running(Response),
    InputRequested(Response),
    Completed(Response),
    ReplacementStarting(Response),
    ReplacementReady(Response),
    Restarted(Response),
}

enum EvaluationStatus {
    Waiting,
    Grace(Duration),
    Report(EvaluationWait),
}

pub(super) struct RestartReservation {
    evaluation: Arc<Evaluation>,
    unfinished: bool,
    completion: Option<CompletionKind>,
    completion_checkpoint: Option<OutputCheckpoint>,
    pub(super) waiting: bool,
}

pub(super) enum RestartDelivery {
    Waiting(mpsc::Receiver<ResponseAcknowledgment>),
    Unclaimed(Response),
}

/// Releases one response-draining claim whenever its `send` path exits.
pub(super) struct WaitClaim {
    evaluation: Arc<Evaluation>,
}

impl Evaluation {
    pub(super) fn new(
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
        output: OutputTape,
    ) -> Self {
        Self {
            state: Mutex::new(EvaluationState {
                phase: EvaluationPhase::Evaluating,
                completion_checkpoint: None,
                completion_collected: false,
                input_report_at: None,
                waiting: false,
                restart_reserved: false,
                restart_handoff: None,
                #[cfg(target_os = "macos")]
                stdin: None,
                pending_stdin: String::new(),
            }),
            changed: tokio::sync::Notify::new(),
            transcript,
            call_id,
            output,
            runtime: tokio::runtime::Handle::current(),
        }
    }

    fn claim_wait(self: &Arc<Self>) -> Result<WaitClaim, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.restart_reserved {
            return Err("session restart began before this send could wait".to_string());
        }
        if state.waiting {
            return Err("worker evaluation is already being polled".to_string());
        }
        state.waiting = true;
        Ok(WaitClaim {
            evaluation: self.clone(),
        })
    }

    /// Reserves an open response until restart finishes retiring the worker.
    pub(super) fn reserve_for_restart(self: &Arc<Self>) -> Result<RestartReservation, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        state.restart_reserved = true;
        if let EvaluationPhase::CellCompletionGrace(deadline) = state.phase {
            drop(state);
            std::thread::sleep(deadline.saturating_duration_since(Instant::now()));
            self.finish_cell_completion_grace();
            state = self
                .state
                .lock()
                .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        }
        let waiting = state.waiting;
        let unfinished = !matches!(state.phase, EvaluationPhase::Complete(_));
        let completion = match state.phase {
            EvaluationPhase::Complete(completion) if !state.completion_collected => {
                Some(completion)
            }
            EvaluationPhase::Complete(_) => None,
            EvaluationPhase::Evaluating | EvaluationPhase::ReplacementStarting => None,
            EvaluationPhase::CellCompletionGrace(_) => {
                unreachable!("completion grace is handled before restart reservation")
            }
        };
        Ok(RestartReservation {
            evaluation: self.clone(),
            unfinished,
            completion,
            completion_checkpoint: completion.and(state.completion_checkpoint),
            waiting,
        })
    }

    /// Queues text and briefly defers any outstanding input report for its receipt.
    pub(super) fn submit_stdin(&self, stdin: String) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if stdin.is_empty() {
            return Ok(());
        }

        if let Some(report_at) = state.input_report_at.as_mut() {
            *report_at = Instant::now() + INPUT_REQUEST_GRACE;
        }
        #[cfg(target_os = "macos")]
        if let Some(writer) = &state.stdin {
            writer.send(stdin)?;
            return Ok(());
        }
        state.pending_stdin.push_str(&stdin);
        Ok(())
    }

    #[cfg(target_os = "macos")]
    pub(super) fn attach_writer(&self, writer: super::platform::StdinSender) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.stdin.is_some() {
            return Err("worker stdin was already attached to this evaluation".to_string());
        }
        if !state.pending_stdin.is_empty() {
            writer.send(std::mem::take(&mut state.pending_stdin))?;
        }
        state.stdin = Some(writer);
        Ok(())
    }

    #[cfg(target_os = "macos")]
    pub(super) fn output(
        &self,
        channel: crate::worker_protocol::ConsoleChannel,
        output: String,
    ) -> Result<(), String> {
        self.output.push_console_text(channel, output);
        Ok(())
    }

    #[cfg(target_os = "macos")]
    pub(super) fn image(&self, data: String, mime_type: String) -> Result<(), String> {
        let artifact = self
            .transcript
            .persist_image(self.call_id, &data, &mime_type)?;
        self.output.push_image(data, mime_type, artifact);
        Ok(())
    }

    #[cfg(target_os = "macos")]
    pub(super) fn input_requested(&self, prompt: String) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.input_report_at.is_some() {
            return Err("worker requested new input before receiving prior input".to_string());
        }
        let prompt = serde_json::to_string(&prompt)
            .map_err(|error| format!("failed to render worker input prompt: {error}"))?;
        self.output
            .push_notice_line(format!("input requested: {prompt}"));
        state.input_report_at = Some(Instant::now() + INPUT_REQUEST_GRACE);
        self.changed.notify_one();
        Ok(())
    }

    #[cfg(target_os = "macos")]
    pub(super) fn resume_input_request(&self) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.input_report_at.is_some() {
            return Err("worker evaluation already has an outstanding input request".to_string());
        }
        let grace = if state.pending_stdin.is_empty() {
            Duration::ZERO
        } else {
            INPUT_REQUEST_GRACE
        };
        state.input_report_at = Some(Instant::now() + grace);
        self.changed.notify_one();
        Ok(())
    }

    #[cfg(target_os = "macos")]
    pub(super) fn input_received(&self) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        state
            .input_report_at
            .take()
            .ok_or_else(|| "worker reported received input without requesting it".to_string())?;
        self.changed.notify_one();
        Ok(())
    }

    #[cfg(target_os = "macos")]
    pub(super) fn input_complete(&self) -> Result<(), String> {
        let state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.input_report_at.is_some() {
            return Err("worker completed with an outstanding input request".to_string());
        }
        Ok(())
    }

    pub(super) fn complete_cell(&self, result: Result<(), SendFailure>) {
        self.complete(result, CompletionKind::Cell, None);
    }

    pub(super) fn complete_cell_after_grace(self: &Arc<Self>) {
        let deadline = Instant::now() + CELL_COMPLETION_GRACE;
        let Ok(mut state) = self.state.lock() else {
            return;
        };
        state.input_report_at = None;
        state.phase = EvaluationPhase::CellCompletionGrace(deadline);
        state.completion_checkpoint = None;
        state.completion_collected = false;
        self.changed.notify_one();
        drop(state);

        let evaluation = self.clone();
        self.runtime.spawn(async move {
            tokio::time::sleep_until(tokio::time::Instant::from_std(deadline)).await;
            evaluation.finish_cell_completion_grace();
        });
    }

    fn finish_cell_completion_grace(&self) {
        let Ok(mut state) = self.state.lock() else {
            return;
        };
        let EvaluationPhase::CellCompletionGrace(deadline) = state.phase else {
            return;
        };
        if Instant::now() < deadline {
            return;
        }
        state.phase = EvaluationPhase::Complete(CompletionKind::Cell);
        state.completion_checkpoint = Some(self.output.checkpoint());
        state.completion_collected = false;
        self.changed.notify_one();
    }

    pub(super) fn reject_new_cell_message(&self) -> &'static str {
        "worker is already evaluating a cell; poll without a code field"
    }

    pub(super) fn reject_preparation_message(&self) -> &'static str {
        "worker is already evaluating a cell; poll it before preparing requirements"
    }

    pub(super) fn start_replacement(&self, failure: SendFailure) {
        let Ok(mut state) = self.state.lock() else {
            return;
        };
        state.input_report_at = None;
        state.completion_checkpoint = None;
        self.output.push_failure(failure);
        state.phase = EvaluationPhase::ReplacementStarting;
        self.changed.notify_one();
    }

    pub(super) fn finish_replacement(&self, result: Result<(), SendFailure>) {
        let completion = if result.is_ok() {
            CompletionKind::ReplacementReady
        } else {
            CompletionKind::ReplacementFailed
        };
        self.complete(result, completion, None);
    }

    fn complete(
        &self,
        result: Result<(), SendFailure>,
        completion: CompletionKind,
        checkpoint: Option<OutputCheckpoint>,
    ) {
        let Ok(mut state) = self.state.lock() else {
            return;
        };
        state.input_report_at = None;
        if let Err(failure) = result
            && (!state.restart_reserved || failure.should_survive_restart())
        {
            self.output.push_failure(failure);
        }
        state.phase = EvaluationPhase::Complete(completion);
        state.completion_checkpoint = checkpoint;
        state.completion_collected = false;
        self.changed.notify_one();
    }

    /// Records whether a worker failure became observable before restart
    /// cancellation took ownership of this evaluation's response.
    pub(super) fn classify_failure(&self, message: String) -> SendFailure {
        let failure = SendFailure::from(message);
        match self.state.lock() {
            Ok(state) if !state.restart_reserved => failure.preceded_restart(),
            Ok(_) | Err(_) => failure,
        }
    }

    pub(super) fn claim(self: &Arc<Self>) -> Result<WaitClaim, String> {
        self.claim_wait()
    }

    pub(super) async fn wait(
        &self,
        _claim: WaitClaim,
        timeout: Duration,
    ) -> Result<EvaluationWait, String> {
        let started = Instant::now();
        loop {
            let changed = self.changed.notified();
            let grace = match self.reported_state(false)? {
                EvaluationStatus::Waiting => None,
                EvaluationStatus::Grace(grace) => Some(grace),
                EvaluationStatus::Report(state) => break Ok(state),
            };
            let remaining = timeout.saturating_sub(started.elapsed());
            if remaining.is_zero() {
                match self.reported_state(true)? {
                    EvaluationStatus::Report(state) => break Ok(state),
                    EvaluationStatus::Waiting => {
                        changed.await;
                        continue;
                    }
                    EvaluationStatus::Grace(grace) => {
                        let _ = tokio::time::timeout(grace, changed).await;
                        continue;
                    }
                }
            }
            let wait = grace.map_or(remaining, |grace| grace.min(remaining));
            if tokio::time::timeout(wait, changed).await.is_err() {
                if grace.is_some_and(|grace| grace <= remaining) {
                    continue;
                }
                match self.reported_state(true)? {
                    EvaluationStatus::Report(state) => break Ok(state),
                    EvaluationStatus::Waiting => continue,
                    EvaluationStatus::Grace(grace) => {
                        let _ = tokio::time::timeout(grace, self.changed.notified()).await;
                        continue;
                    }
                }
            }
        }
    }

    fn reported_state(&self, at_deadline: bool) -> Result<EvaluationStatus, String> {
        self.finish_cell_completion_grace();
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.restart_reserved {
            return Ok(match state.restart_handoff.take() {
                Some(response) => EvaluationStatus::Report(EvaluationWait::Restarted(response)),
                None => EvaluationStatus::Waiting,
            });
        }
        match state.phase {
            EvaluationPhase::Complete(CompletionKind::Cell)
            | EvaluationPhase::Complete(CompletionKind::ReplacementFailed) => {
                state.completion_collected = true;
                let output = state.completion_checkpoint.take().map_or_else(
                    || self.output.take(),
                    |checkpoint| self.output.take_until(checkpoint),
                );
                return Ok(EvaluationStatus::Report(EvaluationWait::Completed(output)));
            }
            EvaluationPhase::Complete(CompletionKind::ReplacementReady) => {
                state.completion_collected = true;
                return Ok(EvaluationStatus::Report(EvaluationWait::ReplacementReady(
                    self.output.take(),
                )));
            }
            EvaluationPhase::CellCompletionGrace(deadline) => {
                return Ok(EvaluationStatus::Grace(
                    deadline.saturating_duration_since(Instant::now()),
                ));
            }
            EvaluationPhase::ReplacementStarting if at_deadline => {
                return Ok(EvaluationStatus::Report(
                    EvaluationWait::ReplacementStarting(self.output.take()),
                ));
            }
            EvaluationPhase::Evaluating | EvaluationPhase::ReplacementStarting => {}
        }
        let Some(report_at) = state.input_report_at else {
            if at_deadline {
                return Ok(EvaluationStatus::Report(EvaluationWait::Running(
                    self.output.take(),
                )));
            }
            return Ok(EvaluationStatus::Waiting);
        };
        let grace = report_at.saturating_duration_since(Instant::now());
        if !at_deadline && !grace.is_zero() {
            return Ok(EvaluationStatus::Grace(grace));
        }
        Ok(EvaluationStatus::Report(EvaluationWait::InputRequested(
            self.output.take(),
        )))
    }
}

impl RestartReservation {
    pub(super) fn unfinished(&self) -> bool {
        self.unfinished
    }

    fn project_response(&self, response: Response) -> Response {
        match self.completion {
            Some(CompletionKind::Cell | CompletionKind::ReplacementFailed) => {
                project_completed(response)
            }
            Some(CompletionKind::ReplacementReady) => project_replacement_ready(response),
            None => response,
        }
    }

    pub(super) fn take_output(&self, output: &OutputTape) -> (Response, Response) {
        match self.completion_checkpoint {
            Some(checkpoint) => (
                self.project_response(output.take_until(checkpoint)),
                output.take(),
            ),
            None => (self.project_response(output.take()), Response::default()),
        }
    }

    pub(super) fn stopped_notice(&self) -> &'static str {
        super::output::EVALUATION_STOPPED_BY_RESTART_NOTICE
    }

    pub(super) fn active_stopped_notice(&self) -> &'static str {
        super::output::ACTIVE_EVALUATION_STOPPED_NOTICE
    }

    pub(super) fn deliver(self, mut response: Response) -> Result<RestartDelivery, String> {
        let mut state = self
            .evaluation
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if !state.waiting {
            state.restart_reserved = false;
            return Ok(RestartDelivery::Unclaimed(response));
        }
        let (acknowledged, wait_for_acknowledgment) = mpsc::sync_channel(0);
        response.acknowledge_with(acknowledged);
        state.restart_handoff = Some(response);
        self.evaluation.changed.notify_one();
        Ok(RestartDelivery::Waiting(wait_for_acknowledgment))
    }
}

impl Drop for RestartReservation {
    fn drop(&mut self) {
        let Ok(mut state) = self.evaluation.state.lock() else {
            return;
        };
        if state.restart_handoff.is_none() {
            state.restart_reserved = false;
            self.evaluation.changed.notify_one();
        }
    }
}

impl Drop for WaitClaim {
    fn drop(&mut self) {
        let Ok(mut state) = self.evaluation.state.lock() else {
            return;
        };
        state.waiting = false;
        let handoff = state.restart_handoff.take();
        self.evaluation.changed.notify_one();
        drop(state);
        drop(handoff);
    }
}
