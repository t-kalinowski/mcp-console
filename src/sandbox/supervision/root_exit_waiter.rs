use super::job_control::SignalRelay;
use super::kqueue::{Kqueue, KqueueWait, ProcessWatchError};
use super::process::{ProcessIdentity, process_identity, process_info};
use std::time::Duration;

const OWNER_WAKE_IDENT: libc::uintptr_t = 1;

pub(super) struct RootExitWaiter {
    kqueue: Kqueue,
    root: ProcessIdentity,
    root_exited: bool,
}

pub(super) struct RootExitWakeup {
    // Keep the queue alive if the waiter exits before another owner-side
    // component reports failure, so a reused descriptor cannot be triggered.
    kqueue: Kqueue,
}

pub(super) enum RootWait {
    Events,
    RootExited,
    Wakeup,
    TimedOut,
}

impl RootExitWakeup {
    pub(super) fn wake(self) -> Result<(), String> {
        self.kqueue
            .trigger_user(OWNER_WAKE_IDENT, "failed to wake sandbox root observer")
    }

    pub(super) fn on_drop(self) -> RootExitWakeGuard {
        RootExitWakeGuard(Some(self))
    }
}

pub(super) struct RootExitWakeGuard(Option<RootExitWakeup>);

impl Drop for RootExitWakeGuard {
    fn drop(&mut self) {
        if let Some(wakeup) = self.0.take() {
            let _ = wakeup.wake();
        }
    }
}

impl RootExitWaiter {
    pub(super) fn start(root_pid: libc::pid_t, signal_relay: &SignalRelay) -> Result<Self, String> {
        let kqueue = Kqueue::new("the sandbox root observer")?;

        let info = process_info(root_pid)?
            .ok_or_else(|| format!("sandbox root {root_pid} exited before exit observation"))?;
        let root = info.identity;
        if !info.is_zombie {
            match kqueue.watch_process(root_pid, libc::NOTE_EXIT) {
                Ok(()) => {}
                Err(ProcessWatchError::Gone) => {
                    return Err(format!(
                        "sandbox root {root_pid} exited before exit observation"
                    ));
                }
                Err(ProcessWatchError::Other(error)) => {
                    return Err(format!("failed to watch sandbox root {root_pid}: {error}"));
                }
            }
            if process_identity(root_pid)? != Some(root) {
                kqueue.remove_process_watch(root_pid);
                return Err(format!(
                    "sandbox root {root_pid} changed before exit observation"
                ));
            }
        }
        kqueue.watch_signals(
            signal_relay.relayed_signals(),
            "failed to watch sandbox launcher signals",
        )?;
        kqueue.watch_user(
            OWNER_WAKE_IDENT,
            "failed to register sandbox root observer wakeup",
        )?;

        Ok(Self {
            kqueue,
            root,
            root_exited: info.is_zombie,
        })
    }

    pub(super) fn wakeup(&self) -> RootExitWakeup {
        RootExitWakeup {
            kqueue: self.kqueue.clone(),
        }
    }

    pub(super) fn wait_for_events(
        &mut self,
        timeout: Option<Duration>,
    ) -> Result<RootWait, String> {
        if self.root_exited {
            return Ok(RootWait::RootExited);
        }

        let events = match self
            .kqueue
            .wait(timeout, "sandbox root observer failed")?
        {
            KqueueWait::Events(events) => events,
            KqueueWait::Interrupted => return Ok(RootWait::Events),
            KqueueWait::TimedOut => return Ok(RootWait::TimedOut),
        };

        let mut owner_wakeup = false;
        for event in &events {
            let event_data = event.data;
            if event.flags & libc::EV_ERROR != 0 {
                if event.filter == libc::EVFILT_PROC
                    && event.ident as libc::pid_t == self.root.pid
                    && event_data == libc::ESRCH as libc::intptr_t
                {
                    self.root_exited = true;
                    continue;
                }
                return Err(format!(
                    "sandbox root observer received event error {event_data}"
                ));
            }

            // Signal filters only wake the launcher loop. SignalRelay inspects
            // and consumes the pending signals before the next wait.
            if event.filter == libc::EVFILT_PROC
                && event.ident as libc::pid_t == self.root.pid
                && event.fflags & libc::NOTE_EXIT != 0
            {
                self.root_exited = true;
            } else if event.filter == libc::EVFILT_USER && event.ident == OWNER_WAKE_IDENT {
                owner_wakeup = true;
            }
        }

        Ok(if self.root_exited {
            RootWait::RootExited
        } else if owner_wakeup {
            RootWait::Wakeup
        } else {
            RootWait::Events
        })
    }
}
