use super::job_control::SignalRelay;
use super::process::{
    ProcessIdentity, list_child_pids, process_identity, process_info, signal_process,
};
use std::collections::{HashMap, HashSet, VecDeque};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::time::{Duration, Instant};

const PROCESS_EXIT_TIMEOUT: Duration = Duration::from_secs(5);
const PROCESS_REAP_CHECK_INTERVAL: Duration = Duration::from_millis(10);
const TRACKER_EVENT_CAPACITY: usize = 32;
#[allow(deprecated)]
const PROCESS_REAP_EVENT: u32 = libc::NOTE_REAP;

pub(super) struct DescendantTracker {
    kqueue: OwnedFd,
    state: TrackerState,
}

pub(super) enum EventWait {
    Events,
    RootExited,
    TimedOut,
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

    pub(super) fn into_parts(self) -> (String, Option<DescendantTracker>) {
        (self.error, self.tracker)
    }
}

impl DescendantTracker {
    pub(super) fn start(
        root_pid: libc::pid_t,
        signal_relay: &SignalRelay,
    ) -> Result<Self, StartFailure> {
        let kqueue_descriptor = unsafe { libc::kqueue() };
        if kqueue_descriptor < 0 {
            return Err(StartFailure::new(format!(
                "failed to create the sandbox process tracker: {}",
                std::io::Error::last_os_error()
            )));
        }
        let kqueue = unsafe { OwnedFd::from_raw_fd(kqueue_descriptor) };

        // Darwin provides neither child subreapers nor PID namespaces, and its
        // kqueue NOTE_TRACK facility is unsupported. A descendant that becomes
        // orphaned before this post-spawn watch, or before a NOTE_FORK event can
        // be paired with a libproc snapshot, is therefore an intentional boundary
        // of the initial launcher. Once observed, descendants that call setsid(),
        // such as processx children, remain tracked by their PID and start time.
        let root = process_identity(root_pid).map_err(StartFailure::new)?;
        let state = TrackerState {
            root,
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
        if let Err(error) = watch_signals(tracker.kqueue.as_raw_fd(), signal_relay) {
            return Err(StartFailure::with_tracker(error, tracker));
        }

        Ok(tracker)
    }

    pub(super) fn wait_for_events(
        &mut self,
        timeout: Option<Duration>,
    ) -> Result<EventWait, String> {
        let mut events: [libc::kevent; TRACKER_EVENT_CAPACITY] =
            unsafe { std::mem::MaybeUninit::zeroed().assume_init() };
        let timeout = timeout.map(|duration| libc::timespec {
            tv_sec: duration.as_secs() as libc::time_t,
            tv_nsec: duration.subsec_nanos() as libc::c_long,
        });
        let timeout = timeout
            .as_ref()
            .map_or(std::ptr::null(), |timeout| timeout as *const _);

        let event_count = unsafe {
            libc::kevent(
                self.kqueue.as_raw_fd(),
                std::ptr::null(),
                0,
                events.as_mut_ptr(),
                events.len() as libc::c_int,
                timeout,
            )
        };
        if event_count < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                return Ok(EventWait::Events);
            }
            return Err(format!("sandbox process tracker failed: {error}"));
        }
        if event_count == 0 {
            return Ok(EventWait::TimedOut);
        }

        let mut root_exited = false;
        for event in events.iter().take(event_count as usize) {
            let pid = event.ident as libc::pid_t;
            let event_data = event.data;
            if event.flags & libc::EV_ERROR != 0 {
                if event.filter == libc::EVFILT_PROC && event_data == libc::ESRCH as libc::intptr_t
                {
                    self.state.active.remove(&pid);
                    root_exited |= self.state.root.is_some_and(|root| root.pid == pid);
                    continue;
                }
                return Err(format!(
                    "sandbox process tracker received event error {event_data}"
                ));
            }

            // Signal filters only wake the loop. SignalRelay inspects and
            // consumes pending signals so inherited masks and ignored signal
            // dispositions retain their normal behavior.
            if event.filter != libc::EVFILT_PROC {
                continue;
            }

            root_exited |= event.fflags & libc::NOTE_EXIT != 0
                && self.state.root.is_some_and(|root| root.pid == pid);
            if event.fflags & PROCESS_REAP_EVENT != 0 {
                // XNU posts NOTE_REAP before removing the PID from its process
                // table. Drop the identity now only when removal has completed.
                // Any coalesced fork is already outside the observation window:
                // its children were reparented before this process was reaped.
                if let Some(identity) = self.state.active.get(&pid).copied()
                    && process_identity(pid)? != Some(identity)
                {
                    self.state.active.remove(&pid);
                }
                continue;
            }
            if event.fflags & libc::NOTE_FORK != 0 {
                add_children(self.kqueue.as_raw_fd(), pid, &mut self.state)?;
            }
        }
        Ok(if root_exited {
            EventWait::RootExited
        } else {
            EventWait::Events
        })
    }

    pub(super) fn terminate_after_root_exit(mut self) -> Result<(), String> {
        let deadline = Instant::now() + PROCESS_EXIT_TIMEOUT;
        let mut cleanup_error = None;
        loop {
            // The root may have exited before wait_for_root() blocked. Consume
            // any queued fork event before removing the root from the snapshot.
            if let Err(error) = self.wait_for_events(Some(Duration::ZERO)) {
                record_first_error(&mut cleanup_error, error);
            }

            // Re-snapshot before each signal pass to narrow the teardown fork
            // window. A child that becomes orphaned before observation remains
            // outside the documented supervision boundary.
            if let Err(error) = discover_active_children(self.kqueue.as_raw_fd(), &mut self.state) {
                record_first_error(&mut cleanup_error, error);
            }
            if let Some(root) = self.state.root
                && self.state.active.get(&root.pid) == Some(&root)
            {
                self.state.active.remove(&root.pid);
            }
            if let Err(error) = remove_stale_processes(&mut self.state.active) {
                record_first_error(&mut cleanup_error, error);
            }

            // The root command has exited, so its background work has no
            // remaining lifetime to preserve.
            for identity in self.state.active.values().copied() {
                if let Err(error) = signal_process(identity, libc::SIGKILL) {
                    record_first_error(&mut cleanup_error, error);
                }
            }
            if let Err(error) = remove_stale_processes(&mut self.state.active) {
                record_first_error(&mut cleanup_error, error);
            }

            if self.state.active.is_empty() {
                return cleanup_error.map_or(Ok(()), Err);
            }

            if Instant::now() >= deadline {
                let timeout = "timed out waiting for sandbox descendants to be reaped".to_string();
                return Err(cleanup_error.map_or(timeout.clone(), |error| {
                    format!("{error}; additionally, {timeout}")
                }));
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            let wait = remaining.min(PROCESS_REAP_CHECK_INTERVAL);
            match self.wait_for_events(Some(wait)) {
                Ok(EventWait::TimedOut) => {
                    if let Err(error) = remove_stale_processes(&mut self.state.active) {
                        record_first_error(&mut cleanup_error, error);
                    }
                    if self.state.active.is_empty() {
                        return cleanup_error.map_or(Ok(()), Err);
                    }
                }
                Ok(EventWait::Events | EventWait::RootExited) => {}
                Err(error) => {
                    record_first_error(&mut cleanup_error, error);
                    std::thread::sleep(wait);
                }
            }
        }
    }
}

struct TrackerState {
    root: Option<ProcessIdentity>,
    active: HashMap<libc::pid_t, ProcessIdentity>,
}

fn remove_stale_processes(
    active: &mut HashMap<libc::pid_t, ProcessIdentity>,
) -> Result<(), String> {
    let identities: Vec<_> = active.values().copied().collect();
    let mut error = None;
    for identity in identities {
        match process_identity(identity.pid) {
            Ok(Some(current)) if current == identity => {}
            Ok(Some(_) | None) => {
                active.remove(&identity.pid);
            }
            Err(process_error) => record_first_error(&mut error, process_error),
        }
    }
    error.map_or(Ok(()), Err)
}

fn record_first_error(current: &mut Option<String>, error: String) {
    if current.is_none() {
        *current = Some(error);
    }
}

fn add_process_tree(
    kqueue: libc::c_int,
    root_pid: libc::pid_t,
    expected_parent: Option<ProcessIdentity>,
    state: &mut TrackerState,
) -> Result<(), String> {
    let mut queue = VecDeque::new();
    queue.push_back((root_pid, expected_parent));
    let mut visited = HashSet::new();

    while let Some((pid, expected_parent)) = queue.pop_front() {
        let Some(info) = process_info(pid)? else {
            continue;
        };
        if let Some(parent) = expected_parent {
            // proc_listchildpids() returns numeric PIDs. Confirm both the
            // parent relationship and parent start time before adopting one,
            // so PID reuse cannot turn an unrelated process into a target.
            if info.parent_pid != parent.pid || process_identity(parent.pid)? != Some(parent) {
                continue;
            }
        }
        let identity = info.identity;
        if !visited.insert(identity) {
            continue;
        }

        if info.is_zombie {
            // macOS cannot attach a reliable process watch after exit, but the
            // bounded teardown scan can still wait for this verified identity.
            state.active.insert(pid, identity);
            continue;
        }

        if state.active.get(&pid) != Some(&identity) {
            state.active.remove(&pid);
            match watch_process(kqueue, pid) {
                Ok(()) => {}
                Err(WatchProcessError::Gone) => continue,
                Err(WatchProcessError::Other(error)) => {
                    return Err(format!("failed to watch sandbox process {pid}: {error}"));
                }
            }

            // Confirm the watch still names the process whose start time was
            // recorded; PID reuse must not turn an old descendant into a new target.
            if process_identity(pid)? != Some(identity) {
                remove_process_watch(kqueue, pid);
                continue;
            }
            state.active.insert(pid, identity);
        }

        queue.extend(
            list_child_pids(pid)?
                .into_iter()
                .map(|child| (child, Some(identity))),
        );
    }

    Ok(())
}

fn add_children(
    kqueue: libc::c_int,
    parent: libc::pid_t,
    state: &mut TrackerState,
) -> Result<(), String> {
    let Some(parent_identity) = state.active.get(&parent).copied() else {
        return Ok(());
    };
    for child in list_child_pids(parent)? {
        add_process_tree(kqueue, child, Some(parent_identity), state)?;
    }
    Ok(())
}

fn discover_active_children(kqueue: libc::c_int, state: &mut TrackerState) -> Result<(), String> {
    let parents: Vec<_> = state.active.values().copied().collect();
    for parent in parents {
        let Some(info) = process_info(parent.pid)? else {
            continue;
        };
        if info.identity == parent && !info.is_zombie {
            add_children(kqueue, parent.pid, state)?;
        }
    }
    Ok(())
}

enum WatchProcessError {
    Gone,
    Other(std::io::Error),
}

fn watch_signals(kqueue: libc::c_int, signal_relay: &SignalRelay) -> Result<(), String> {
    let events: Vec<_> = signal_relay
        .relayed_signals()
        .map(|signal| libc::kevent {
            ident: signal as libc::uintptr_t,
            filter: libc::EVFILT_SIGNAL,
            flags: libc::EV_ADD | libc::EV_CLEAR,
            fflags: 0,
            data: 0,
            udata: std::ptr::null_mut(),
        })
        .collect();

    if events.is_empty() {
        return Ok(());
    }

    loop {
        let result = unsafe {
            libc::kevent(
                kqueue,
                events.as_ptr(),
                events.len() as libc::c_int,
                std::ptr::null_mut(),
                0,
                std::ptr::null(),
            )
        };
        if result >= 0 {
            return Ok(());
        }

        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(format!("failed to watch sandbox launcher signals: {error}"));
        }
    }
}

fn watch_process(kqueue: libc::c_int, pid: libc::pid_t) -> Result<(), WatchProcessError> {
    let event = libc::kevent {
        ident: pid as libc::uintptr_t,
        filter: libc::EVFILT_PROC,
        flags: libc::EV_ADD | libc::EV_CLEAR,
        // NOTE_REAP is deprecated for ordinary exit observation, but NOTE_EXIT
        // fires while the PID may still exist as a zombie. Requesting both
        // keeps the watch installed until the process is reaped. XNU posts the
        // event before removing the PID from its process table, so teardown also
        // verifies that the recorded process identity has disappeared.
        fflags: libc::NOTE_FORK | libc::NOTE_EXIT | PROCESS_REAP_EVENT,
        data: 0,
        udata: std::ptr::null_mut(),
    };
    let result =
        unsafe { libc::kevent(kqueue, &event, 1, std::ptr::null_mut(), 0, std::ptr::null()) };
    if result >= 0 {
        return Ok(());
    }

    let error = std::io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::ESRCH) {
        Err(WatchProcessError::Gone)
    } else {
        Err(WatchProcessError::Other(error))
    }
}

fn remove_process_watch(kqueue: libc::c_int, pid: libc::pid_t) {
    let event = libc::kevent {
        ident: pid as libc::uintptr_t,
        filter: libc::EVFILT_PROC,
        flags: libc::EV_DELETE,
        fflags: 0,
        data: 0,
        udata: std::ptr::null_mut(),
    };
    let _ = unsafe { libc::kevent(kqueue, &event, 1, std::ptr::null_mut(), 0, std::ptr::null()) };
}
