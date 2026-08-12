use std::sync::Mutex;
use std::time::{Duration, Instant};

use super::output::{OutputTape, Response, SendFailure};

const INPUT_REQUEST_GRACE: Duration = Duration::from_millis(10);

pub(super) struct Evaluation {
    state: Mutex<EvaluationState>,
    changed: tokio::sync::Notify,
    transcript: crate::transcript::Transcript,
    call_id: u64,
    output: OutputTape,
}

struct EvaluationState {
    completed: bool,
    input_report_at: Option<Instant>,
    waiting: bool,
    cancelled_by_restart: bool,
    #[cfg(target_os = "macos")]
    stdin: Option<super::platform::StdinSender>,
    pending_stdin: Vec<u8>,
}

pub(super) enum EvaluationWait {
    Running(Response),
    InputRequested(Response),
    Completed(Response),
    Restarted,
}

enum EvaluationStatus {
    Waiting,
    Grace(Duration),
    Report(EvaluationWait),
}

impl Evaluation {
    pub(super) fn new(
        transcript: crate::transcript::Transcript,
        call_id: u64,
        output: OutputTape,
    ) -> Self {
        Self {
            state: Mutex::new(EvaluationState {
                completed: false,
                input_report_at: None,
                waiting: false,
                cancelled_by_restart: false,
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

    pub(super) fn claim_wait(&self) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.cancelled_by_restart {
            return Err("session restarted before the operation completed".to_string());
        }
        if state.waiting {
            return Err("worker evaluation is already being polled".to_string());
        }
        state.waiting = true;
        Ok(())
    }

    /// Prevents this evaluation from draining output after restart takes ownership.
    pub(super) fn cancel_for_restart(&self) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        state.cancelled_by_restart = true;
        self.changed.notify_one();
        Ok(())
    }

    /// Queues bytes and briefly defers any outstanding input report for its receipt.
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
    pub(super) fn output(&self, output: String) -> Result<(), String> {
        self.output.push_text(output);
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
            .push_line(format!("[input requested: {prompt}]\n"));
        state.input_report_at = Some(Instant::now() + INPUT_REQUEST_GRACE);
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

    pub(super) fn complete(&self, result: Result<(), SendFailure>) {
        let Ok(mut state) = self.state.lock() else {
            return;
        };
        state.input_report_at = None;
        if let Err(failure) = result
            && (!state.cancelled_by_restart || failure.worker_stopped)
        {
            self.output.push_failure(failure);
        }
        state.completed = true;
        self.changed.notify_one();
    }

    pub(super) async fn wait(&self, timeout: Duration) -> Result<EvaluationWait, String> {
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
                break self.state_at_deadline();
            }
            let wait = grace.map_or(remaining, |grace| grace.min(remaining));
            if tokio::time::timeout(wait, changed).await.is_err() {
                if grace.is_some_and(|grace| grace <= remaining) {
                    continue;
                }
                break self.state_at_deadline();
            }
        }
    }

    fn state_at_deadline(&self) -> Result<EvaluationWait, String> {
        match self.reported_state(true)? {
            EvaluationStatus::Report(state) => Ok(state),
            EvaluationStatus::Waiting | EvaluationStatus::Grace(_) => {
                unreachable!("the deadline makes every evaluation state reportable")
            }
        }
    }

    fn reported_state(&self, at_deadline: bool) -> Result<EvaluationStatus, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.cancelled_by_restart {
            state.waiting = false;
            return Ok(EvaluationStatus::Report(EvaluationWait::Restarted));
        }
        if state.completed {
            state.waiting = false;
            return Ok(EvaluationStatus::Report(EvaluationWait::Completed(
                self.output.take(),
            )));
        }
        let Some(report_at) = state.input_report_at else {
            if at_deadline {
                state.waiting = false;
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
        state.waiting = false;
        Ok(EvaluationStatus::Report(EvaluationWait::InputRequested(
            self.output.take(),
        )))
    }
}
