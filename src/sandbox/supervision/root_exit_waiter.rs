use super::job_control::SignalRelay;
use super::kqueue::{Kqueue, KqueueWait, ProcessWatchError};
use super::process::{ProcessIdentity, process_identity, process_info};
use std::time::Duration;

const OWNER_WAKE_IDENT: libc::uintptr_t = 1;

#[derive(Clone, Copy)]
pub(in crate::sandbox) struct SandboxOwner(ProcessIdentity);

pub(in crate::sandbox) struct RootExitWaiter {
    kqueue: Kqueue,
    root: ProcessIdentity,
    root_exited: bool,
    owner: Option<ProcessIdentity>,
    owner_exited: bool,
}

pub(in crate::sandbox) struct RootExitWakeup {
    // Keep the queue alive if the waiter exits before another owner-side
    // component reports failure, so a reused descriptor cannot be triggered.
    kqueue: Kqueue,
}

pub(super) enum RootWait {
    Events,
    OwnerExited,
    RootExited,
    Wakeup,
    TimedOut,
}

impl SandboxOwner {
    pub(in crate::sandbox) fn capture(owner_pid: u32) -> Result<Self, String> {
        let owner_pid = libc::pid_t::try_from(owner_pid)
            .ok()
            .filter(|pid| *pid > 0)
            .ok_or_else(|| "sandbox owner PID is invalid".to_string())?;
        if unsafe { libc::getppid() } != owner_pid {
            return Err(format!(
                "sandbox owner {owner_pid} is not the launcher's current parent"
            ));
        }
        let info = process_info(owner_pid)?
            .filter(|info| !info.is_zombie)
            .ok_or_else(|| format!("sandbox owner {owner_pid} exited before exit observation"))?;
        Ok(Self(info.identity))
    }
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
    pub(in crate::sandbox) fn start(
        root_pid: libc::pid_t,
        signal_relay: &SignalRelay,
        owner: Option<SandboxOwner>,
    ) -> Result<Self, String> {
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
        let owner = match owner {
            Some(SandboxOwner(owner)) => {
                match kqueue.watch_process(owner.pid, libc::NOTE_EXIT) {
                    Ok(()) => {}
                    Err(ProcessWatchError::Gone) => {
                        return Err(format!(
                            "sandbox owner {} exited before exit observation",
                            owner.pid
                        ));
                    }
                    Err(ProcessWatchError::Other(error)) => {
                        return Err(format!(
                            "failed to watch sandbox owner {}: {error}",
                            owner.pid
                        ));
                    }
                }
                if unsafe { libc::getppid() } != owner.pid
                    || process_identity(owner.pid)? != Some(owner)
                {
                    kqueue.remove_process_watch(owner.pid);
                    return Err(format!(
                        "sandbox owner {} changed before exit observation",
                        owner.pid
                    ));
                }
                Some(owner)
            }
            None => None,
        };
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
            owner,
            owner_exited: false,
        })
    }

    pub(in crate::sandbox) fn wakeup(&self) -> RootExitWakeup {
        RootExitWakeup {
            kqueue: self.kqueue.clone(),
        }
    }

    pub(in crate::sandbox) fn validate_owner(&self) -> Result<(), String> {
        let Some(owner) = self.owner else {
            return Ok(());
        };
        if unsafe { libc::getppid() } != owner.pid {
            return Err(format!(
                "sandbox owner {} changed before target release",
                owner.pid
            ));
        }
        let info = process_info(owner.pid)?;
        if !info.is_some_and(|info| info.identity == owner && !info.is_zombie) {
            return Err(format!(
                "sandbox owner {} changed before target release",
                owner.pid
            ));
        }
        Ok(())
    }

    pub(super) fn wait_for_events(
        &mut self,
        timeout: Option<Duration>,
    ) -> Result<RootWait, String> {
        if self.root_exited {
            return Ok(RootWait::RootExited);
        }
        if self.owner_exited {
            return Ok(RootWait::OwnerExited);
        }

        let events = match self.kqueue.wait(timeout, "sandbox root observer failed")? {
            KqueueWait::Events(events) => events,
            KqueueWait::Interrupted => return Ok(RootWait::Events),
            KqueueWait::TimedOut => return Ok(RootWait::TimedOut),
        };

        let mut owner_wakeup = false;
        for event in &events {
            let event_data = event.data;
            if event.flags & libc::EV_ERROR != 0 {
                if event.filter == libc::EVFILT_PROC && event_data == libc::ESRCH as libc::intptr_t
                {
                    let pid = event.ident as libc::pid_t;
                    if pid == self.root.pid {
                        self.root_exited = true;
                        continue;
                    }
                    if self.owner.is_some_and(|owner| owner.pid == pid) {
                        self.owner_exited = true;
                        continue;
                    }
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
            } else if event.filter == libc::EVFILT_PROC
                && self
                    .owner
                    .is_some_and(|owner| event.ident as libc::pid_t == owner.pid)
                && event.fflags & libc::NOTE_EXIT != 0
            {
                self.owner_exited = true;
            } else if event.filter == libc::EVFILT_USER && event.ident == OWNER_WAKE_IDENT {
                owner_wakeup = true;
            }
        }

        Ok(if self.root_exited {
            RootWait::RootExited
        } else if owner_wakeup {
            RootWait::Wakeup
        } else if self.owner_exited {
            RootWait::OwnerExited
        } else {
            RootWait::Events
        })
    }
}
