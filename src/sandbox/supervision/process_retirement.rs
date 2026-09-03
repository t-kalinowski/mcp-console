use super::kqueue::KqueueWait;
use super::process::{process_identity, process_info, signal_process};
use super::process_tracker::{DescendantTracker, EventWait, TrackerEvents};
use super::process_tree::{
    PROCESS_REAP_EVENT, add_children, discover_active_children, record_first_error,
    remove_stale_processes,
};
use std::time::{Duration, Instant};

const PROCESS_RETIREMENT_CHECK_INTERVAL: Duration = Duration::from_millis(10);

impl DescendantTracker {
    pub(super) fn stop(self, retirement_grace: Duration) -> Result<(), String> {
        self.terminate(false, retirement_grace)
    }

    pub(super) fn wait_for_events(
        &mut self,
        timeout: Option<Duration>,
    ) -> Result<EventWait, String> {
        let events = match self
            .kqueue
            .wait(timeout, "sandbox process tracker failed")?
        {
            KqueueWait::Events(events) => events,
            KqueueWait::Interrupted => return Ok(EventWait::Events(TrackerEvents::default())),
            KqueueWait::TimedOut => return Ok(EventWait::TimedOut),
        };

        let mut observed = TrackerEvents::default();
        for event in &events {
            let pid = event.ident as libc::pid_t;
            let event_data = event.data;
            if event.flags & libc::EV_ERROR != 0 {
                if event.filter == libc::EVFILT_PROC && event_data == libc::ESRCH as libc::intptr_t
                {
                    self.state.active.remove(&pid);
                    observed.root_exited |= self.state.root.pid == pid;
                    continue;
                }
                return Err(format!(
                    "sandbox process tracker received event error {event_data}"
                ));
            }
            if event.filter != libc::EVFILT_PROC {
                if event.filter == libc::EVFILT_READ
                    && self.control_descriptor == libc::c_int::try_from(event.ident).ok()
                {
                    observed.control_readable = true;
                    continue;
                }
                return Err("sandbox process tracker received an unexpected event".to_string());
            }

            observed.root_exited |=
                event.fflags & libc::NOTE_EXIT != 0 && self.state.root.pid == pid;
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
                add_children(&self.kqueue, pid, &mut self.state)?;
            }
        }
        Ok(EventWait::Events(observed))
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
                Ok(EventWait::Events(events)) => root_exited |= events.root_exited,
                Ok(EventWait::TimedOut) => {}
                Err(error) => record_first_error(&mut cleanup_error, error),
            }

            // Re-snapshot before each signal pass to narrow the teardown fork
            // window. A child that becomes orphaned before observation remains
            // outside the documented supervision boundary.
            if let Err(error) = discover_active_children(&self.kqueue, &mut self.state) {
                record_first_error(&mut cleanup_error, error);
            }
            match self.root_has_exited() {
                Ok(exited) => root_exited |= exited,
                Err(error) => record_first_error(&mut cleanup_error, error),
            }
            let root = self.state.root;
            if root_exited && self.state.active.get(&root.pid) == Some(&root) {
                self.state.active.remove(&root.pid);
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
                Ok(EventWait::Events(_)) => {}
                Err(error) => {
                    record_first_error(&mut cleanup_error, error);
                    std::thread::sleep(wait);
                }
            }
        }
    }

    pub(super) fn root_has_exited(&self) -> Result<bool, String> {
        let root = self.state.root;
        let Some(info) = process_info(root.pid)? else {
            return Ok(true);
        };
        Ok(info.identity != root || info.is_zombie)
    }
}
