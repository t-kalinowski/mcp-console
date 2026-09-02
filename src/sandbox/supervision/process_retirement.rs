use super::process::{process_identity, process_info, signal_process};
use super::process_tracker::{DescendantTracker, EventWait};
use super::process_tree::{
    PROCESS_REAP_EVENT, add_children, discover_active_children, record_first_error,
    remove_stale_processes,
};
use std::os::fd::AsRawFd;
use std::time::{Duration, Instant};

const PROCESS_RETIREMENT_CHECK_INTERVAL: Duration = Duration::from_millis(10);
const TRACKER_EVENT_CAPACITY: usize = 32;

impl DescendantTracker {
    pub(super) fn stop(self, retirement_grace: Duration) -> Result<(), String> {
        self.terminate(false, retirement_grace)
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

    pub(super) fn terminate(
        mut self,
        mut root_exited: bool,
        retirement_grace: Duration,
    ) -> Result<(), String> {
        let mut retirement_deadline = None;
        let mut cleanup_error = None;
        loop {
            // The root may have exited before the event wait blocked. Consume
            // any queued fork event before removing it from the snapshot.
            match self.wait_for_events(Some(Duration::ZERO)) {
                Ok(EventWait::RootExited) => root_exited = true,
                Ok(EventWait::Events | EventWait::TimedOut) => {}
                Err(error) => record_first_error(&mut cleanup_error, error),
            }

            // Re-snapshot before each signal pass to narrow the teardown fork
            // window. A child that becomes orphaned before observation remains
            // outside the documented supervision boundary.
            if let Err(error) = discover_active_children(self.kqueue.as_raw_fd(), &mut self.state) {
                record_first_error(&mut cleanup_error, error);
            }
            match self.root_has_exited() {
                Ok(exited) => root_exited |= exited,
                Err(error) => record_first_error(&mut cleanup_error, error),
            }
            if root_exited
                && let Some(root) = self.state.root
                && self.state.active.get(&root.pid) == Some(&root)
            {
                self.state.active.remove(&root.pid);
            }
            if let Err(error) = remove_stale_processes(&mut self.state.active) {
                record_first_error(&mut cleanup_error, error);
            }

            // The root command has exited, so its background work has no
            // remaining lifetime to preserve. Continue through individual
            // failures so one inaccessible identity does not abandon the rest.
            let identities: Vec<_> = self.state.active.values().copied().collect();
            for identity in identities {
                match signal_process(identity, libc::SIGKILL) {
                    Ok(true) => {}
                    Ok(false) => {
                        // Missing, reused, and zombie identities have no live
                        // process left to retire. Do not spend the bounded grace
                        // waiting for an external parent to reap a zombie.
                        self.state.active.remove(&identity.pid);
                    }
                    Err(error) => record_first_error(&mut cleanup_error, error),
                }
            }
            if let Err(error) = remove_stale_processes(&mut self.state.active) {
                record_first_error(&mut cleanup_error, error);
            }

            if self.state.active.is_empty() {
                return cleanup_error.map_or(Ok(()), Err);
            }

            // Discovery and signaling scale with the number of observed
            // processes. Never abandon known identities mid-pass. Once every
            // identity has received a signal pass, bound the grace for stopping.
            let deadline =
                *retirement_deadline.get_or_insert_with(|| Instant::now() + retirement_grace);
            if Instant::now() >= deadline {
                let timeout = "timed out waiting for sandbox descendants to stop".to_string();
                return Err(cleanup_error.map_or(timeout.clone(), |error| {
                    format!("{error}; additionally, {timeout}")
                }));
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            let wait = remaining.min(PROCESS_RETIREMENT_CHECK_INTERVAL);
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

    pub(super) fn root_has_exited(&self) -> Result<bool, String> {
        let Some(root) = self.state.root else {
            return Ok(true);
        };
        let Some(info) = process_info(root.pid)? else {
            return Ok(true);
        };
        Ok(info.identity != root || info.is_zombie)
    }
}
