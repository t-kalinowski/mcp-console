#[path = "supervision/process.rs"]
mod process;
#[path = "supervision/process_retirement.rs"]
mod process_retirement;
#[path = "supervision/process_tracker.rs"]
mod process_tracker;
#[path = "supervision/process_tree.rs"]
mod process_tree;

use super::{file_descriptors, platform};
use std::process::{Child, Command, ExitCode};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, mpsc};
use std::thread::{self, JoinHandle};
use std::time::Duration;

const PROCESS_OBSERVATION_POLL_INTERVAL: Duration = Duration::from_millis(10);
const PROCESS_REAP_GRACE: Duration = Duration::from_secs(1);

/// A sandbox process tree observed from one direct root.
///
/// Darwin cannot atomically install a descendant observer at spawn time. A
/// descendant that becomes orphaned before the post-spawn root watch or a
/// corresponding fork observation remains outside this lifetime. A dedicated
/// observer continuously consumes fork events while the generation is live;
/// once a process is observed, retirement follows its PID and start time across
/// process-group and session changes.
pub(crate) struct ObservedLifetime {
    stop_requested: Arc<AtomicBool>,
    observer: Option<JoinHandle<Option<ObservationResult>>>,
}

struct ObservationResult {
    tracker: process_tracker::DescendantTracker,
    error: Option<String>,
}

impl ObservedLifetime {
    pub(crate) fn start(root_pid: u32) -> Result<Self, String> {
        let root_pid = libc::pid_t::try_from(root_pid)
            .ok()
            .filter(|pid| *pid > 0)
            .ok_or_else(|| "sandbox process tracker received an invalid root PID".to_string())?;
        let tracker = process_tracker::DescendantTracker::start(root_pid)
            .map_err(|failure| failure.retire(PROCESS_REAP_GRACE))?;

        // Transfer the tracker only after the observer thread exists, so a
        // thread-spawn failure can still retire every identity already seen.
        let stop_requested = Arc::new(AtomicBool::new(false));
        let observer_stop = Arc::clone(&stop_requested);
        let (tracker_sender, tracker_receiver) = mpsc::channel();
        let observer = match thread::Builder::new()
            .name("mcp-console-sandbox-observer".to_string())
            .spawn(move || {
                let Ok(mut tracker) = tracker_receiver.recv() else {
                    return None;
                };
                let mut error = None;
                while !observer_stop.load(Ordering::Acquire) {
                    if let Err(observation_error) =
                        tracker.wait_for_events(Some(PROCESS_OBSERVATION_POLL_INTERVAL))
                    {
                        error = Some(observation_error);
                        break;
                    }
                }
                Some(ObservationResult { tracker, error })
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
            observer: Some(observer),
        })
    }

    /// Stops the root and every descendant observed from it.
    pub(crate) fn stop(mut self) -> Result<(), String> {
        self.stop_requested.store(true, Ordering::Release);
        let observer = self
            .observer
            .take()
            .expect("active observed lifetime should retain its observer");
        let observation = observer
            .join()
            .map_err(|_| "sandbox process observer panicked".to_string())?
            .ok_or_else(|| "sandbox process observer stopped before tracker handoff".to_string())?;
        let cleanup = observation.tracker.terminate(false, PROCESS_REAP_GRACE);
        match (observation.error, cleanup) {
            (None, Ok(())) => Ok(()),
            (Some(error), Ok(())) | (None, Err(error)) => Err(error),
            (Some(error), Err(cleanup_error)) => {
                Err(format!("{error}; additionally, {cleanup_error}"))
            }
        }
    }
}

impl Drop for ObservedLifetime {
    fn drop(&mut self) {
        // Dropping SandboxedChild intentionally does not terminate its process.
        // Stop only the detached observer thread; explicit force_stop owns
        // process retirement and joins the observer before cleaning up files.
        self.stop_requested.store(true, Ordering::Release);
    }
}

fn retire_after_observer_failure(
    tracker: process_tracker::DescendantTracker,
    error: String,
) -> String {
    match tracker.terminate(false, PROCESS_REAP_GRACE) {
        Ok(()) => error,
        Err(cleanup_error) => format!("{error}; additionally, {cleanup_error}"),
    }
}

pub(super) fn status(
    mut sandbox_command: Command,
    temporary_directory: platform::TemporaryDirectory,
) -> Result<ExitCode, String> {
    // The standalone path has no private transport descriptors, so a
    // parent-side snapshot is sufficient before it starts any threads.
    file_descriptors::close_unlisted(&mut sandbox_command)?;
    let mut child = sandbox_command
        .spawn()
        .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;

    let tracker = match process_tracker::DescendantTracker::start(child.id() as libc::pid_t) {
        Ok(tracker) => tracker,
        Err(failure) => {
            let error = failure.retire(PROCESS_REAP_GRACE);
            let error = stop_direct_child(&mut child, error);
            preserve(temporary_directory);
            return Err(error);
        }
    };
    if let Err(error) = tracker.supervise(PROCESS_REAP_GRACE) {
        let error = stop_direct_child(&mut child, error);
        preserve(temporary_directory);
        return Err(error);
    }

    let status = child
        .wait()
        .map_err(|error| format!("failed to wait for `{}`: {error}", platform::SANDBOX_EXEC))?;
    Ok(platform::exit_code(status))
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

    match platform::wait_for_process_exit_without_reaping(child.id(), PROCESS_REAP_GRACE) {
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
