use super::process::{process_identity, process_info};
use super::process_tree::{TrackerState, add_process_tree};
use std::collections::HashMap;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::sync::Arc;
use std::time::Duration;

const OBSERVER_WAKE_IDENT: libc::uintptr_t = 1;

pub(super) struct DescendantTracker {
    pub(super) kqueue: Arc<OwnedFd>,
    pub(super) state: TrackerState,
}

pub(super) struct ObserverWakeup {
    // Keep the queue alive if the observer exits before its owner requests
    // stop, so a reused descriptor cannot receive the wakeup.
    kqueue: Arc<OwnedFd>,
}

impl ObserverWakeup {
    pub(super) fn wake(self) -> Result<(), String> {
        let event = libc::kevent {
            ident: OBSERVER_WAKE_IDENT,
            filter: libc::EVFILT_USER,
            flags: 0,
            fflags: libc::NOTE_TRIGGER,
            data: 0,
            udata: std::ptr::null_mut(),
        };
        submit_observer_event(
            self.kqueue.as_raw_fd(),
            &event,
            "failed to wake sandbox process observer",
        )
    }
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
    TimedOut,
}

impl DescendantTracker {
    pub(super) fn start(root_pid: libc::pid_t) -> Result<Self, StartFailure> {
        let kqueue_descriptor = unsafe { libc::kqueue() };
        if kqueue_descriptor < 0 {
            return Err(StartFailure::new(format!(
                "failed to create the sandbox process tracker: {}",
                std::io::Error::last_os_error()
            )));
        }
        let kqueue = Arc::new(unsafe { OwnedFd::from_raw_fd(kqueue_descriptor) });

        // Darwin provides neither child subreapers nor PID namespaces, and its
        // kqueue NOTE_TRACK facility is unsupported. A descendant that becomes
        // orphaned before this post-spawn watch, or before a NOTE_FORK event can
        // be paired with a libproc snapshot, is therefore outside this tracker's
        // intentional boundary. Once observed, descendants that call setsid(),
        // such as processx children, remain tracked by PID and start time.
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
        if let Err(error) = add_process_tree(
            tracker.kqueue.as_raw_fd(),
            root_pid,
            None,
            &mut tracker.state,
        ) {
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

    pub(super) fn register_observer_wakeup(&self) -> Result<ObserverWakeup, String> {
        let event = libc::kevent {
            ident: OBSERVER_WAKE_IDENT,
            filter: libc::EVFILT_USER,
            flags: libc::EV_ADD | libc::EV_CLEAR,
            fflags: 0,
            data: 0,
            udata: std::ptr::null_mut(),
        };
        submit_observer_event(
            self.kqueue.as_raw_fd(),
            &event,
            "failed to register sandbox process observer wakeup",
        )?;
        Ok(ObserverWakeup {
            kqueue: Arc::clone(&self.kqueue),
        })
    }

    /// Waits for the root to exit, marks the transition to owner-controlled
    /// retirement, and then terminates the observed lifetime.
    ///
    /// The callback runs exactly once before the first termination pass. Crash
    /// owners use that point to preserve the private directory if the normal
    /// owner disappears while local cleanup is in progress.
    pub(super) fn supervise(
        mut self,
        retirement_grace: Duration,
        retirement_started: impl FnOnce(),
    ) -> Result<(), String> {
        let observation = match self.root_has_exited() {
            Ok(true) => Ok(()),
            Ok(false) => loop {
                match self.wait_for_events(None) {
                    Ok(EventWait::RootExited) => break Ok(()),
                    Ok(EventWait::Events | EventWait::TimedOut) => {}
                    Err(error) => break Err(error),
                }
            },
            Err(error) => Err(error),
        };
        retirement_started();

        match observation {
            Ok(()) => self.terminate(true, retirement_grace),
            Err(error) => match self.terminate(false, retirement_grace) {
                Ok(()) => Err(error),
                Err(cleanup_error) => Err(format!("{error}; additionally, {cleanup_error}")),
            },
        }
    }
}

fn submit_observer_event(
    kqueue: libc::c_int,
    event: &libc::kevent,
    description: &str,
) -> Result<(), String> {
    loop {
        let result =
            unsafe { libc::kevent(kqueue, event, 1, std::ptr::null_mut(), 0, std::ptr::null()) };
        if result >= 0 {
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(format!("{description}: {error}"));
        }
    }
}
