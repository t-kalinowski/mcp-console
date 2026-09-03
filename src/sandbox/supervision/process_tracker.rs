use super::kqueue::Kqueue;
use super::process::{process_identity, process_info};
use super::process_tree::{TrackerState, add_process_tree};
use std::collections::HashMap;
use std::time::Duration;

pub(super) struct DescendantTracker {
    pub(super) kqueue: Kqueue,
    pub(super) state: TrackerState,
    pub(super) control_descriptor: Option<libc::c_int>,
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

#[derive(Default)]
pub(super) struct TrackerEvents {
    pub(super) control_readable: bool,
    pub(super) root_exited: bool,
}

pub(super) enum EventWait {
    Events(TrackerEvents),
    TimedOut,
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
        let mut tracker = Self {
            kqueue,
            state,
            control_descriptor: None,
        };
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

    pub(super) fn watch_control(&mut self, descriptor: libc::c_int) -> Result<(), String> {
        assert!(
            self.control_descriptor.is_none(),
            "sandbox manager control can be watched only once"
        );
        self.kqueue
            .watch_read(descriptor, "failed to watch sandbox manager control")?;
        self.control_descriptor = Some(descriptor);
        Ok(())
    }

    pub(super) fn remove_control_watch(&mut self) {
        if let Some(descriptor) = self.control_descriptor.take() {
            self.kqueue.remove_read_watch(descriptor);
        }
    }

    pub(super) fn supervise(mut self, retirement_grace: Duration) -> Result<(), String> {
        let observation = match self.root_has_exited() {
            Ok(true) => Ok(()),
            Ok(false) => loop {
                match self.wait_for_events(None) {
                    Ok(EventWait::Events(events)) if events.root_exited => break Ok(()),
                    Ok(EventWait::Events(_) | EventWait::TimedOut) => {}
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
