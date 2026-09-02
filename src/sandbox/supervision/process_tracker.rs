use super::kqueue::{Kqueue, WeakKqueue};
use super::process::{process_identity, process_info};
use super::process_tree::{TrackerState, add_process_tree};
use std::collections::HashMap;
use std::time::Duration;

pub(super) const TRACKER_STOP_IDENT: libc::uintptr_t = 1;

pub(super) struct DescendantTracker {
    pub(super) kqueue: Kqueue,
    pub(super) state: TrackerState,
}

pub(super) struct TrackerStopWakeup {
    kqueue: WeakKqueue,
}

pub(super) struct StartFailure {
    error: String,
    tracker: Option<DescendantTracker>,
}

impl StartFailure {
    fn new(error: String) -> Self {
        Self {
            error,
            tracker: None,
        }
    }

    fn with_tracker(error: String, tracker: DescendantTracker) -> Self {
        Self {
            error,
            tracker: Some(tracker),
        }
    }

    pub(super) fn retire(self, retirement_grace: Duration) -> String {
        match self.tracker {
            Some(tracker) => match tracker.terminate(false, retirement_grace) {
                Ok(()) => self.error,
                Err(cleanup_error) => {
                    format!("{}; additionally, {cleanup_error}", self.error)
                }
            },
            None => self.error,
        }
    }
}

pub(super) enum EventWait {
    Events,
    RootExited,
    StopRequested,
    TimedOut,
}

impl TrackerStopWakeup {
    pub(super) fn wake(self) -> Result<(), String> {
        self.kqueue
            .trigger_user(TRACKER_STOP_IDENT, "failed to wake sandbox process tracker")
    }
}

impl DescendantTracker {
    pub(super) fn start(root_pid: libc::pid_t) -> Result<Self, StartFailure> {
        let kqueue = Kqueue::new("the sandbox process tracker").map_err(StartFailure::new)?;

        // Darwin provides neither child subreapers nor PID namespaces, and its
        // kqueue NOTE_TRACK facility is unsupported. Supported callers prevent
        // the target from running until this root watch is installed. After
        // release, a descendant orphaned before a NOTE_FORK event can be paired
        // with a libproc snapshot is outside this tracker's intentional boundary.
        // Once observed, descendants that call setsid(), such as processx
        // children, remain tracked by PID and start time.
        let root = process_identity(root_pid)
            .map_err(StartFailure::new)?
            .ok_or_else(|| {
                StartFailure::new(format!(
                    "sandbox root {root_pid} exited before descendant tracking"
                ))
            })?;
        let state = TrackerState {
            root: Some(root),
            active: HashMap::new(),
        };
        let mut tracker = Self { kqueue, state };
        if let Err(error) = add_process_tree(&tracker.kqueue, root_pid, None, &mut tracker.state) {
            return Err(StartFailure::with_tracker(error, tracker));
        }
        if tracker.state.active.get(&root_pid) != Some(&root) {
            let root_exited = match process_info(root_pid) {
                Ok(info) => info.is_some_and(|info| info.identity == root && info.is_zombie),
                Err(error) => return Err(StartFailure::with_tracker(error, tracker)),
            };
            if root_exited {
                // A very short-lived command can exit between the first
                // identity snapshot and watch registration. Its waitable
                // zombie still pins the identity even though there is no
                // remaining execution to observe.
                tracker.state.active.insert(root_pid, root);
            } else {
                return Err(StartFailure::with_tracker(
                    format!("sandbox root {root_pid} changed before descendant tracking"),
                    tracker,
                ));
            }
        }

        Ok(tracker)
    }

    pub(super) fn stop_wakeup(&self) -> Result<TrackerStopWakeup, String> {
        self.kqueue.watch_user(
            TRACKER_STOP_IDENT,
            "failed to register sandbox process tracker stop request",
        )?;
        Ok(TrackerStopWakeup {
            kqueue: self.kqueue.downgrade(),
        })
    }

    pub(super) fn supervise(mut self, retirement_grace: Duration) -> Result<(), String> {
        let observation = match self.root_has_exited() {
            Ok(true) => Ok(()),
            Ok(false) => loop {
                match self.wait_for_events(None) {
                    Ok(EventWait::RootExited) => break Ok(()),
                    Ok(EventWait::StopRequested) => return self.stop(retirement_grace),
                    Ok(EventWait::Events | EventWait::TimedOut) => {}
                    Err(error) => break Err(error),
                }
            },
            Err(error) => Err(error),
        };

        match observation {
            Ok(()) => self.terminate(true, retirement_grace),
            Err(error) => match self.terminate(false, retirement_grace) {
                Ok(()) => Err(error),
                Err(cleanup_error) => Err(format!("{error}; additionally, {cleanup_error}")),
            },
        }
    }
}
