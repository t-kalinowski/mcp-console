#[path = "supervision/manager.rs"]
mod manager;
#[path = "supervision/process.rs"]
mod process;
#[path = "supervision/process_retirement.rs"]
mod process_retirement;
#[path = "supervision/process_tracker.rs"]
mod process_tracker;
#[path = "supervision/process_tree.rs"]
mod process_tree;
#[path = "supervision/standalone.rs"]
mod standalone;

pub(crate) use self::manager::{CleanupPreparation, SandboxManager};
use super::platform;
use std::os::unix::net::UnixStream;
use std::process::{Child, Command, ExitCode};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, mpsc};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const PROCESS_STALE_PRUNE_INTERVAL: Duration = Duration::from_secs(1);
const PROCESS_RETIREMENT_GRACE: Duration = Duration::from_secs(1);

pub(super) fn run_manager() -> Result<(), String> {
    manager::run()
}

/// A sandbox process tree observed from one direct root.
///
/// Darwin cannot atomically install a descendant observer at spawn time. A
/// descendant that becomes orphaned before the post-spawn root watch or a
/// corresponding fork observation remains outside this lifetime. A dedicated
/// observer continuously consumes fork events while the generation is live;
/// once a process is observed, retirement follows its PID and start time across
/// process-group and session changes. Lifecycle control wakes the blocking
/// kqueue wait through an explicit user event.
pub(crate) struct ObservedLifetime {
    stop_requested: Arc<AtomicBool>,
    observer_wakeup: Option<process_tracker::ObserverWakeup>,
    observer: Option<JoinHandle<Option<ObservationResult>>>,
}

enum ObservationResult {
    Tracker {
        tracker: process_tracker::DescendantTracker,
        error: Option<String>,
    },
    Retired {
        error: String,
    },
}

impl ObservedLifetime {
    pub(crate) fn start(root_pid: u32) -> Result<Self, String> {
        let root_pid = libc::pid_t::try_from(root_pid)
            .ok()
            .filter(|pid| *pid > 0)
            .ok_or_else(|| "sandbox process tracker received an invalid root PID".to_string())?;
        let tracker = process_tracker::DescendantTracker::start(root_pid)
            .map_err(|failure| failure.retire(PROCESS_RETIREMENT_GRACE))?;
        let observer_wakeup = match tracker.register_observer_wakeup() {
            Ok(observer_wakeup) => observer_wakeup,
            Err(error) => return Err(retire_after_observer_failure(tracker, error)),
        };

        // Transfer the tracker only after the observer thread exists, so a
        // thread-spawn failure can still retire every identity already seen.
        let stop_requested = Arc::new(AtomicBool::new(false));
        let observer_stop = Arc::clone(&stop_requested);
        let (tracker_sender, tracker_receiver) =
            mpsc::sync_channel::<process_tracker::DescendantTracker>(0);
        let observer = match thread::Builder::new()
            .name("mcp-console-sandbox-observer".to_string())
            .spawn(move || {
                let Ok(mut tracker) = tracker_receiver.recv() else {
                    return None;
                };
                let mut error = None;
                let mut next_stale_prune = Instant::now() + PROCESS_STALE_PRUNE_INTERVAL;
                loop {
                    if observer_stop.load(Ordering::Acquire) {
                        break;
                    }
                    let wait = next_stale_prune.saturating_duration_since(Instant::now());
                    if let Err(observation_error) = tracker.wait_for_events(Some(wait)) {
                        return Some(ObservationResult::Retired {
                            error: retire_after_observer_failure(tracker, observation_error),
                        });
                    }
                    if observer_stop.load(Ordering::Acquire) {
                        break;
                    }
                    if Instant::now() >= next_stale_prune {
                        if let Err(observation_error) = tracker.prune_stale_processes() {
                            error.get_or_insert(observation_error);
                        }
                        next_stale_prune = Instant::now() + PROCESS_STALE_PRUNE_INTERVAL;
                    }
                }
                Some(ObservationResult::Tracker { tracker, error })
            }) {
            Ok(observer) => observer,
            Err(spawn_error) => {
                return Err(retire_after_observer_failure(
                    tracker,
                    format!("failed to start sandbox process observer: {spawn_error}"),
                ));
            }
        };

        if let Err(send_error) = tracker_sender.send(tracker) {
            stop_requested.store(true, Ordering::Release);
            let _ = observer.join();
            return Err(retire_after_observer_failure(
                send_error.0,
                "sandbox process observer stopped before tracker handoff".to_string(),
            ));
        }

        Ok(Self {
            stop_requested,
            observer_wakeup: Some(observer_wakeup),
            observer: Some(observer),
        })
    }

    /// Stops the root and every descendant observed from it.
    pub(crate) fn stop(mut self) -> Result<(), String> {
        let wake_error = self.request_observer_stop().err();
        let observer = self
            .observer
            .take()
            .expect("active observed lifetime should retain its observer");
        let result = match observer.join() {
            Err(_) => Err("sandbox process observer panicked".to_string()),
            Ok(None) => Err("sandbox process observer stopped before tracker handoff".to_string()),
            Ok(Some(ObservationResult::Retired { error })) => Err(error),
            Ok(Some(ObservationResult::Tracker { tracker, error })) => {
                with_prior_error(error, tracker.terminate(false, PROCESS_RETIREMENT_GRACE))
            }
        };
        with_prior_error(wake_error, result)
    }

    fn request_observer_stop(&mut self) -> Result<(), String> {
        self.stop_requested.store(true, Ordering::Release);
        self.observer_wakeup
            .take()
            .map_or(Ok(()), process_tracker::ObserverWakeup::wake)
    }
}

impl Drop for ObservedLifetime {
    fn drop(&mut self) {
        // Dropping SandboxedChild intentionally does not terminate its process.
        // Stop only the detached observer thread; explicit force_stop owns
        // process retirement and joins the observer before cleaning up files.
        let _ = self.request_observer_stop();
    }
}

fn with_prior_error(prior: Option<String>, result: Result<(), String>) -> Result<(), String> {
    match (prior, result) {
        (None, result) => result,
        (Some(error), Ok(())) => Err(error),
        (Some(error), Err(additional)) => Err(additional_error(error, additional)),
    }
}

fn retire_after_observer_failure(
    tracker: process_tracker::DescendantTracker,
    error: String,
) -> String {
    match tracker.terminate(false, PROCESS_RETIREMENT_GRACE) {
        Ok(()) => error,
        Err(cleanup_error) => format!("{error}; additionally, {cleanup_error}"),
    }
}

pub(super) fn status(
    sandbox_command: Command,
    temporary_directory: platform::TemporaryDirectory,
    target_gate: UnixStream,
    launcher_gate: UnixStream,
) -> Result<ExitCode, String> {
    standalone::status(
        sandbox_command,
        temporary_directory,
        target_gate,
        launcher_gate,
    )
}

pub(super) fn stop_direct_child(child: &mut Child, primary: String) -> String {
    let mut error = primary;
    match child.try_wait() {
        Ok(Some(_)) => return error,
        Ok(None) => {}
        Err(status_error) => {
            error = additional_error(
                error,
                format!(
                    "failed to inspect direct `{}` during cleanup: {status_error}",
                    platform::SANDBOX_EXEC
                ),
            );
        }
    }

    if let Err(kill_error) = child.kill()
        && kill_error.raw_os_error() != Some(libc::ESRCH)
    {
        return additional_error(
            error,
            format!(
                "failed to stop direct `{}` during cleanup: {kill_error}",
                platform::SANDBOX_EXEC
            ),
        );
    }

    match platform::wait_for_process_exit_without_reaping(child.id(), PROCESS_RETIREMENT_GRACE) {
        Ok(true) => {}
        Ok(false) => {
            return additional_error(
                error,
                format!(
                    "timed out waiting for direct `{}` to stop",
                    platform::SANDBOX_EXEC
                ),
            );
        }
        Err(wait_error) => {
            return additional_error(
                error,
                format!(
                    "failed to observe direct `{}` during cleanup: {wait_error}",
                    platform::SANDBOX_EXEC
                ),
            );
        }
    }
    if let Err(wait_error) = child.wait() {
        error = additional_error(
            error,
            format!(
                "failed to reap direct `{}` during cleanup: {wait_error}",
                platform::SANDBOX_EXEC
            ),
        );
    }
    error
}

fn additional_error(primary: String, additional: String) -> String {
    format!("{primary}; additionally, {additional}")
}

fn preserve(directory: platform::TemporaryDirectory) {
    // A live unobserved descendant may still use this path. Deliberately leak
    // the guard after cleanup failure rather than deleting files underneath it.
    std::mem::forget(directory);
}
