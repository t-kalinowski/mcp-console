use std::sync::{Arc, Mutex, mpsc};
use std::time::{Duration, Instant};

use super::output::{
    OutputCut, OutputTape, Response, ResponseAcknowledgment, SendFailure, project_completed,
    project_controlled_completed, project_replacement_ready,
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
    completion_cut: Option<OutputCut>,
    /// Prior operation and lifecycle output transferred into this cell's response.
    control_prelude: Option<Response>,
    /// Idle output captured immediately before this cell was admitted.
    idle_prelude: Option<Response>,
    /// A response returned by the server but not claimed by its MCP transport.
    reclaimed: Option<Response>,
    /// Delivery of the most recently assembled response, until one owner settles it.
    delivery: Option<mpsc::Receiver<ResponseAcknowledgment>>,
    /// Whether a waiter already drained the response for a completion phase.
    completion_collected: bool,
    /// Whether successful cell completion must end with an explicit final marker.
    controlled_completion: bool,
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
    Reclaimed(Response),
    Restarted(Response),
}

enum EvaluationStatus {
    Waiting,
    Grace(Duration),
    Report(EvaluationWait),
}

pub(super) struct EvaluationReservation {
    evaluation: Arc<Evaluation>,
    unfinished: bool,
    project_completion: bool,
    controlled_completion: bool,
    completion: Option<CompletionKind>,
    completion_cut: Option<OutputCut>,
    reclaimed: Option<Response>,
    delivery: Option<mpsc::Receiver<ResponseAcknowledgment>>,
    pub(super) waiting: bool,
}

pub(super) enum RestartDelivery {
    Waiting(mpsc::Receiver<ResponseAcknowledgment>),
    Unclaimed(Response),
}

pub(super) struct RestartDeliveryFailure {
    pub(super) message: String,
    pub(super) response: Response,
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
        control_prelude: Response,
        idle_prelude: Response,
        controlled_completion: bool,
    ) -> Self {
        Self {
            state: Mutex::new(EvaluationState {
                phase: EvaluationPhase::Evaluating,
                completion_cut: None,
                control_prelude: Some(control_prelude),
                idle_prelude: Some(idle_prelude),
                reclaimed: None,
                delivery: None,
                completion_collected: false,
                controlled_completion,
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
        if !state.settle_delivery()? {
            return Err("previous send response delivery is still pending".to_string());
        }
        if state.completion_collected && state.reclaimed.is_none() {
            return Err("evaluation response was already delivered".to_string());
        }
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

    pub(super) fn is_interruptible(&self) -> Result<bool, String> {
        let state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        Ok(matches!(
            state.phase,
            EvaluationPhase::Evaluating | EvaluationPhase::ReplacementStarting
        ))
    }

    /// Reserves an open response until restart finishes retiring the worker.
    pub(super) fn reserve_for_restart(self: &Arc<Self>) -> Result<EvaluationReservation, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if let EvaluationPhase::CellCompletionGrace(deadline) = state.phase {
            drop(state);
            std::thread::sleep(deadline.saturating_duration_since(Instant::now()));
            self.finish_cell_completion_grace();
            state = self
                .state
                .lock()
                .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        }
        state.restart_reserved = true;
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
        Ok(EvaluationReservation {
            evaluation: self.clone(),
            unfinished,
            project_completion: true,
            controlled_completion: state.controlled_completion,
            completion,
            completion_cut: completion.and(state.completion_cut),
            reclaimed: state.reclaimed.take(),
            delivery: state.delivery.take(),
            waiting,
        })
    }

    /// Reserves a completed response for a following cell without adding a terminal marker.
    pub(super) fn reserve_completed_for_handoff(
        self: &Arc<Self>,
    ) -> Result<Option<EvaluationReservation>, String> {
        self.reserve_completed(false)
    }

    /// Reserves a completed response for direct delivery with its original terminal marker.
    pub(super) fn reserve_completed_for_delivery(
        self: &Arc<Self>,
    ) -> Result<Option<EvaluationReservation>, String> {
        self.reserve_completed(true)
    }

    fn reserve_completed(
        self: &Arc<Self>,
        project_completion: bool,
    ) -> Result<Option<EvaluationReservation>, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if let EvaluationPhase::CellCompletionGrace(deadline) = state.phase {
            drop(state);
            std::thread::sleep(deadline.saturating_duration_since(Instant::now()));
            self.finish_cell_completion_grace();
            state = self
                .state
                .lock()
                .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        }
        let EvaluationPhase::Complete(completion_kind) = state.phase else {
            return Ok(None);
        };
        state.restart_reserved = true;
        let completion = (!state.completion_collected).then_some(completion_kind);
        Ok(Some(EvaluationReservation {
            evaluation: self.clone(),
            unfinished: false,
            project_completion,
            controlled_completion: state.controlled_completion,
            completion,
            completion_cut: completion.and(state.completion_cut),
            reclaimed: state.reclaimed.take(),
            delivery: state.delivery.take(),
            waiting: state.waiting,
        }))
    }

    /// Reaps a completed evaluation only after its assembled response was delivered.
    pub(super) fn reap_delivered_completion(&self) -> Result<bool, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if !state.settle_delivery()? {
            return Ok(false);
        }
        Ok(matches!(state.phase, EvaluationPhase::Complete(_))
            && state.completion_collected
            && state.delivery.is_none()
            && state.reclaimed.is_none()
            && !state.waiting
            && !state.restart_reserved)
    }

    /// Adopts output that remained idle until the worker operation became active.
    pub(super) fn capture_prelude_before(&self, boundary: impl FnOnce()) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        let additional = self.output.take_prelude_before(boundary);
        if let Some(prelude) = state.idle_prelude.as_mut() {
            prelude.extend(additional);
        } else {
            state.idle_prelude = Some(additional);
        }
        Ok(())
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
    pub(super) fn bounded_notice(&self, message: String) -> Result<(), String> {
        self.output.push_bounded_notice_line(message);
        Ok(())
    }

    #[cfg(target_os = "macos")]
    pub(super) fn image(&self, data: String, mime_type: String) -> Result<(), String> {
        crate::transcript::validate_image_data(&data)?;
        self.output
            .push_image_with_artifact(data, mime_type, |data, mime_type| {
                self.transcript.persist_image(self.call_id, data, mime_type)
            })
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
        // Give raw-output readers a brief window to publish cross-pipe output that
        // arrived with Completed. The cut still leaves later observations for the
        // next response; it does not claim cross-pipe chronology.
        let deadline = Instant::now() + CELL_COMPLETION_GRACE;
        let Ok(mut state) = self.state.lock() else {
            return;
        };
        state.input_report_at = None;
        state.phase = EvaluationPhase::CellCompletionGrace(deadline);
        state.completion_cut = None;
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
        state.completion_cut = Some(self.output.completion_cut());
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
        state.completion_cut = None;
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
        cut: Option<OutputCut>,
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
        state.completion_cut = Some(cut.unwrap_or_else(|| {
            if matches!(completion, CompletionKind::Cell) {
                self.output.completion_cut()
            } else {
                self.output.cut()
            }
        }));
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
        if let Some(mut response) = state.reclaimed.take() {
            state.track_delivery(&mut response);
            return Ok(EvaluationStatus::Report(EvaluationWait::Reclaimed(
                response,
            )));
        }
        match state.phase {
            EvaluationPhase::Complete(
                completion @ (CompletionKind::Cell | CompletionKind::ReplacementFailed),
            ) => {
                state.completion_collected = true;
                let cut = state
                    .completion_cut
                    .take()
                    .expect("a completed evaluation must retain its output cut");
                let mut output = take_owned_response(&mut state, &self.output, cut);
                if matches!(completion, CompletionKind::Cell) && state.controlled_completion {
                    output = project_controlled_completed(output);
                }
                state.track_delivery(&mut output);
                return Ok(EvaluationStatus::Report(EvaluationWait::Completed(output)));
            }
            EvaluationPhase::Complete(CompletionKind::ReplacementReady) => {
                state.completion_collected = true;
                let cut = state
                    .completion_cut
                    .take()
                    .expect("a completed replacement must retain its output cut");
                let mut output = take_owned_response(&mut state, &self.output, cut);
                state.track_delivery(&mut output);
                return Ok(EvaluationStatus::Report(EvaluationWait::ReplacementReady(
                    output,
                )));
            }
            EvaluationPhase::CellCompletionGrace(deadline) => {
                return Ok(EvaluationStatus::Grace(
                    deadline.saturating_duration_since(Instant::now()),
                ));
            }
            EvaluationPhase::ReplacementStarting if at_deadline => {
                let cut = self.output.cut();
                let mut output = take_owned_response(&mut state, &self.output, cut);
                state.track_delivery(&mut output);
                return Ok(EvaluationStatus::Report(
                    EvaluationWait::ReplacementStarting(output),
                ));
            }
            EvaluationPhase::Evaluating | EvaluationPhase::ReplacementStarting => {}
        }
        let Some(report_at) = state.input_report_at else {
            if at_deadline {
                let cut = self.output.cut();
                let mut output = take_owned_response(&mut state, &self.output, cut);
                state.track_delivery(&mut output);
                return Ok(EvaluationStatus::Report(EvaluationWait::Running(output)));
            }
            return Ok(EvaluationStatus::Waiting);
        };
        let grace = report_at.saturating_duration_since(Instant::now());
        if !at_deadline && !grace.is_zero() {
            return Ok(EvaluationStatus::Grace(grace));
        }
        let cut = self.output.cut();
        let mut output = take_owned_response(&mut state, &self.output, cut);
        state.track_delivery(&mut output);
        Ok(EvaluationStatus::Report(EvaluationWait::InputRequested(
            output,
        )))
    }
}

impl EvaluationState {
    fn track_delivery(&mut self, response: &mut Response) {
        assert!(
            self.delivery.is_none(),
            "an evaluation can have only one response awaiting delivery"
        );
        let (acknowledgment, delivered) = mpsc::sync_channel(1);
        response.acknowledge_with(acknowledgment);
        self.delivery = Some(delivered);
    }

    fn settle_delivery(&mut self) -> Result<bool, String> {
        let Some(delivery) = self.delivery.as_ref() else {
            return Ok(true);
        };
        match delivery.try_recv() {
            Ok(ResponseAcknowledgment::Delivered) => {
                self.delivery = None;
                Ok(true)
            }
            Ok(ResponseAcknowledgment::Unclaimed(response)) => {
                self.delivery = None;
                assert!(
                    self.reclaimed.replace(response).is_none(),
                    "an evaluation can reclaim only one response at a time"
                );
                Ok(true)
            }
            Err(mpsc::TryRecvError::Empty) => Ok(false),
            Err(mpsc::TryRecvError::Disconnected) => {
                Err("previous send response delivery acknowledgment stopped".to_string())
            }
        }
    }
}

impl EvaluationReservation {
    pub(super) fn unfinished(&self) -> bool {
        self.unfinished
    }

    fn project_response(&self, response: Response) -> Response {
        if !self.project_completion {
            return response;
        }
        match self.completion {
            Some(CompletionKind::Cell) => {
                if self.controlled_completion {
                    project_controlled_completed(response)
                } else {
                    project_completed(response)
                }
            }
            Some(CompletionKind::ReplacementFailed) => project_completed(response),
            Some(CompletionKind::ReplacementReady) => project_replacement_ready(response),
            None => response,
        }
    }

    pub(super) fn take_output(&mut self, output: &OutputTape) -> (Response, Response) {
        let cut = self
            .completion_cut
            .unwrap_or_else(|| output.completion_cut());
        let mut state = self
            .evaluation
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let current_output = take_owned_response(&mut state, output, cut);
        drop(state);
        let post_completion = if self.completion_cut.is_some() {
            output.take_generation()
        } else {
            Response::default()
        };
        let current_output = self.project_response(current_output);
        let mut old_output = self.reclaimed.take().unwrap_or_default();
        old_output.extend_logical_region(current_output);
        (old_output, post_completion)
    }

    /// Transfers an already assembled response's delivery claim to restart.
    pub(super) fn take_pending_delivery(
        &mut self,
    ) -> Option<mpsc::Receiver<ResponseAcknowledgment>> {
        self.delivery.take()
    }

    pub(super) fn stopped_notice(&self) -> &'static str {
        super::output::EVALUATION_STOPPED_BY_RESTART_NOTICE
    }

    pub(super) fn active_stopped_notice(&self) -> &'static str {
        super::output::ACTIVE_EVALUATION_STOPPED_NOTICE
    }

    pub(super) fn deliver(
        self,
        mut response: Response,
    ) -> Result<RestartDelivery, RestartDeliveryFailure> {
        let mut state = match self.evaluation.state.lock() {
            Ok(state) => state,
            Err(_) => {
                return Err(RestartDeliveryFailure {
                    message: "worker evaluation state lock poisoned".to_string(),
                    response,
                });
            }
        };
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

fn take_owned_response(
    state: &mut EvaluationState,
    output: &OutputTape,
    cut: OutputCut,
) -> Response {
    let mut cell = output.drain_through(cut);
    if let Some(mut idle) = state.idle_prelude.take() {
        idle.extend_cell_after_idle_prelude(cell);
        cell = idle;
    }
    let Some(mut control) = state.control_prelude.take() else {
        return cell;
    };
    control.extend_logical_region(cell);
    control
}

impl Drop for EvaluationReservation {
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::worker_client::output::{Content, SendResponse, render_response};

    async fn completed_response() -> (Arc<Evaluation>, Response) {
        let output = OutputTape::new();
        let evaluation = Arc::new(Evaluation::new(
            crate::transcript::Transcript::new(),
            None,
            output,
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
        (
            evaluation,
            render_response(SendResponse::Completed(response)),
        )
    }

    fn response_text(response: Response) -> String {
        let (content, is_error, delivery) = response.into_parts();
        assert!(!is_error);
        assert!(delivery.is_none());
        let [Content::Text(text)] = content.as_slice() else {
            panic!("expected one text response block")
        };
        text.clone()
    }

    #[tokio::test]
    async fn completed_evaluation_remains_until_its_response_is_delivered() {
        let (evaluation, response) = completed_response().await;
        assert!(!evaluation.reap_delivered_completion().unwrap());

        let (_, _, delivery) = response.into_parts();
        delivery.unwrap().delivered();

        assert!(evaluation.reap_delivered_completion().unwrap());
    }

    #[tokio::test]
    async fn unclaimed_completed_response_is_replayed_once() {
        let (evaluation, response) = completed_response().await;
        let (_, _, delivery) = response.into_parts();
        delivery.unwrap().unclaimed();
        assert!(!evaluation.reap_delivered_completion().unwrap());

        let claim = evaluation.claim().unwrap();
        let EvaluationWait::Reclaimed(response) =
            evaluation.wait(claim, Duration::ZERO).await.unwrap()
        else {
            panic!("unclaimed response was not replayed")
        };
        let response = render_response(SendResponse::Restarted(response));
        let (content, is_error, delivery) = response.into_parts();
        assert!(!is_error);
        assert!(matches!(content.as_slice(), [Content::Text(text)] if text == "[done]"));
        delivery.unwrap().delivered();

        assert!(evaluation.reap_delivered_completion().unwrap());
    }

    #[tokio::test]
    async fn restart_claims_an_assembled_response_until_delivery_resolves() {
        let (evaluation, response) = completed_response().await;
        let mut restart = evaluation.reserve_for_restart().unwrap();
        let (_, post_completion) = restart.take_output(&evaluation.output);
        let (post_completion, is_error, delivery) = post_completion.into_parts();
        assert!(post_completion.is_empty());
        assert!(!is_error);
        assert!(delivery.is_none());
        let delivery_result = restart.take_pending_delivery().unwrap();
        assert!(matches!(
            delivery_result.try_recv(),
            Err(mpsc::TryRecvError::Empty)
        ));

        let (_, _, delivery) = response.into_parts();
        delivery.unwrap().unclaimed();
        let ResponseAcknowledgment::Unclaimed(response) = delivery_result.recv().unwrap() else {
            panic!("cancelled response was reported as delivered")
        };
        assert_eq!(response_text(response), "[done]");
        assert!(delivery_result.try_recv().is_err());
    }
}
