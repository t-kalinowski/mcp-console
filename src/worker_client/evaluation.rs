use std::sync::{Arc, Mutex, mpsc};
use std::time::{Duration, Instant};

use super::output::{
    OutputCheckpoint, OutputTape, Response, ResponseAcknowledgment, SendFailure, project_completed,
    project_replacement_ready,
};

pub(super) struct Evaluation {
    state: Mutex<EvaluationState>,
    changed: tokio::sync::Notify,
    transcript: crate::transcript::Transcript,
    call_id: Option<u64>,
    output: OutputTape,
}

struct EvaluationState {
    phase: EvaluationPhase,
    completion_checkpoint: Option<OutputCheckpoint>,
    /// Whether a waiter already drained the response for a terminal phase.
    completion_collected: bool,
    input_requested: bool,
    /// Whether one `send` currently owns the right to drain this evaluation's response.
    waiting: bool,
    restart_reserved: bool,
    restart_handoff: Option<Response>,
    #[cfg(target_os = "macos")]
    stdin: Option<super::platform::StdinSender>,
    pending_stdin: Vec<u8>,
}

#[derive(Clone, Copy)]
enum EvaluationPhase {
    Evaluating,
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
    Report(EvaluationWait),
}

pub(super) struct RestartReservation {
    evaluation: Arc<Evaluation>,
    unfinished: bool,
    completion: Option<CompletionKind>,
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
                input_requested: false,
                waiting: false,
                restart_reserved: false,
                restart_handoff: None,
                #[cfg(target_os = "macos")]
                stdin: None,
                pending_stdin: Vec::new(),
            }),
            changed: tokio::sync::Notify::new(),
            transcript,
            call_id,
            output,
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
        let unfinished = !matches!(state.phase, EvaluationPhase::Complete(_));
        let completion = match state.phase {
            EvaluationPhase::Complete(completion) if !state.completion_collected => {
                Some(completion)
            }
            EvaluationPhase::Complete(_) => None,
            EvaluationPhase::Evaluating | EvaluationPhase::ReplacementStarting => None,
        };
        let waiting = state.waiting;
        Ok(RestartReservation {
            evaluation: self.clone(),
            unfinished,
            completion,
            waiting,
        })
    }

    /// Queues exact bytes without treating submission as receipt.
    pub(super) fn submit_stdin(&self, stdin: String) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if stdin.is_empty() {
            return Ok(());
        }

        let bytes = stdin.into_bytes();
        #[cfg(target_os = "macos")]
        if let Some(writer) = &state.stdin {
            writer.send(bytes)?;
            return Ok(());
        }
        state.pending_stdin.extend(bytes);
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
        if state.input_requested {
            return Err("worker requested new input before receiving prior input".to_string());
        }
        let prompt = serde_json::to_string(&prompt)
            .map_err(|error| format!("failed to render worker input prompt: {error}"))?;
        self.output
            .push_notice_line(format!("input requested: {prompt}"));
        state.input_requested = true;
        self.changed.notify_one();
        Ok(())
    }

    #[cfg(target_os = "macos")]
    pub(super) fn resume_input_request(&self) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.input_requested {
            return Err("worker evaluation already has an outstanding input request".to_string());
        }
        state.input_requested = true;
        self.changed.notify_one();
        Ok(())
    }

    #[cfg(target_os = "macos")]
    pub(super) fn input_received(&self) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if !state.input_requested {
            return Err("worker reported received input without requesting it".to_string());
        }
        state.input_requested = false;
        self.changed.notify_one();
        Ok(())
    }

    #[cfg(target_os = "macos")]
    pub(super) fn input_complete(&self) -> Result<(), String> {
        let state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.input_requested {
            return Err("worker completed with an outstanding input request".to_string());
        }
        Ok(())
    }

    pub(super) fn complete_cell(&self, result: Result<(), SendFailure>) {
        self.complete(result, CompletionKind::Cell, None);
    }

    pub(super) fn complete_cell_at(&self, checkpoint: OutputCheckpoint) {
        self.complete(Ok(()), CompletionKind::Cell, Some(checkpoint));
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
        state.input_requested = false;
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
        state.input_requested = false;
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
            match self.reported_state(false)? {
                EvaluationStatus::Waiting => {}
                EvaluationStatus::Report(state) => break Ok(state),
            }
            let remaining = timeout.saturating_sub(started.elapsed());
            if remaining.is_zero() {
                match self.reported_state(true)? {
                    EvaluationStatus::Report(state) => break Ok(state),
                    EvaluationStatus::Waiting => {
                        changed.await;
                        continue;
                    }
                }
            }
            if tokio::time::timeout(remaining, changed).await.is_err() {
                match self.reported_state(true)? {
                    EvaluationStatus::Report(state) => break Ok(state),
                    EvaluationStatus::Waiting => continue,
                }
            }
        }
    }

    fn reported_state(&self, at_deadline: bool) -> Result<EvaluationStatus, String> {
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
            EvaluationPhase::ReplacementStarting if at_deadline => {
                return Ok(EvaluationStatus::Report(
                    EvaluationWait::ReplacementStarting(self.output.take()),
                ));
            }
            EvaluationPhase::Evaluating | EvaluationPhase::ReplacementStarting => {}
        }
        if !at_deadline {
            return Ok(EvaluationStatus::Waiting);
        }
        if state.input_requested {
            Ok(EvaluationStatus::Report(EvaluationWait::InputRequested(
                self.output.take(),
            )))
        } else {
            Ok(EvaluationStatus::Report(EvaluationWait::Running(
                self.output.take(),
            )))
        }
    }
}

impl RestartReservation {
    pub(super) fn unfinished(&self) -> bool {
        self.unfinished
    }

    pub(super) fn project_response(&self, response: Response) -> Response {
        match self.completion {
            Some(CompletionKind::Cell | CompletionKind::ReplacementFailed) => {
                project_completed(response)
            }
            Some(CompletionKind::ReplacementReady) => project_replacement_ready(response),
            None => response,
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
