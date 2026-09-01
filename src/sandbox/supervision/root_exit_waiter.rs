use super::job_control::SignalRelay;
use super::process::{ProcessIdentity, process_identity, process_info};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::sync::Arc;

const EVENT_CAPACITY: usize = 32;
const OWNER_WAKE_IDENT: libc::uintptr_t = 1;

pub(super) struct RootExitWaiter {
    kqueue: Arc<OwnedFd>,
    root: ProcessIdentity,
    root_exited: bool,
}

pub(super) struct RootExitWakeup {
    // Keep the queue alive if the waiter exits before another owner-side
    // component reports failure, so a reused descriptor cannot be triggered.
    kqueue: Arc<OwnedFd>,
}

pub(super) enum RootWait {
    Events,
    RootExited,
    Wakeup,
}

impl RootExitWakeup {
    pub(super) fn wake(self) -> Result<(), String> {
        let event = libc::kevent {
            ident: OWNER_WAKE_IDENT,
            filter: libc::EVFILT_USER,
            flags: 0,
            fflags: libc::NOTE_TRIGGER,
            data: 0,
            udata: std::ptr::null_mut(),
        };
        submit_event(
            self.kqueue.as_raw_fd(),
            &event,
            "failed to wake sandbox root observer",
        )
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
        let kqueue_descriptor = unsafe { libc::kqueue() };
        if kqueue_descriptor < 0 {
            return Err(format!(
                "failed to create the sandbox root observer: {}",
                std::io::Error::last_os_error()
            ));
        }
        let kqueue = Arc::new(unsafe { OwnedFd::from_raw_fd(kqueue_descriptor) });

        let info = process_info(root_pid)?
            .ok_or_else(|| format!("sandbox root {root_pid} exited before exit observation"))?;
        let root = info.identity;
        if !info.is_zombie {
            match watch_root_exit(kqueue.as_raw_fd(), root_pid) {
                Ok(()) => {}
                Err(WatchProcessError::Gone) => {
                    return Err(format!(
                        "sandbox root {root_pid} exited before exit observation"
                    ));
                }
                Err(WatchProcessError::Other(error)) => {
                    return Err(format!("failed to watch sandbox root {root_pid}: {error}"));
                }
            }
            if process_identity(root_pid)? != Some(root) {
                remove_process_watch(kqueue.as_raw_fd(), root_pid);
                return Err(format!(
                    "sandbox root {root_pid} changed before exit observation"
                ));
            }
        }
        watch_signals(kqueue.as_raw_fd(), signal_relay)?;
        watch_owner_wakeup(kqueue.as_raw_fd())?;

        Ok(Self {
            kqueue,
            root,
            root_exited: info.is_zombie,
        })
    }

    pub(super) fn wakeup(&self) -> RootExitWakeup {
        RootExitWakeup {
            kqueue: Arc::clone(&self.kqueue),
        }
    }

    pub(super) fn wait_for_events(&mut self) -> Result<RootWait, String> {
        if self.root_exited {
            return Ok(RootWait::RootExited);
        }

        let mut events: [libc::kevent; EVENT_CAPACITY] =
            unsafe { std::mem::MaybeUninit::zeroed().assume_init() };

        let event_count = unsafe {
            libc::kevent(
                self.kqueue.as_raw_fd(),
                std::ptr::null(),
                0,
                events.as_mut_ptr(),
                events.len() as libc::c_int,
                std::ptr::null(),
            )
        };
        if event_count < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                return Ok(RootWait::Events);
            }
            return Err(format!("sandbox root observer failed: {error}"));
        }
        if event_count == 0 {
            return Ok(RootWait::Events);
        }

        let mut owner_wakeup = false;
        for event in events.iter().take(event_count as usize) {
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

fn watch_owner_wakeup(kqueue: libc::c_int) -> Result<(), String> {
    let event = libc::kevent {
        ident: OWNER_WAKE_IDENT,
        filter: libc::EVFILT_USER,
        flags: libc::EV_ADD | libc::EV_CLEAR,
        fflags: 0,
        data: 0,
        udata: std::ptr::null_mut(),
    };
    submit_event(
        kqueue,
        &event,
        "failed to register sandbox root observer wakeup",
    )
}

fn submit_event(
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

enum WatchProcessError {
    Gone,
    Other(std::io::Error),
}

fn watch_root_exit(kqueue: libc::c_int, pid: libc::pid_t) -> Result<(), WatchProcessError> {
    let event = libc::kevent {
        ident: pid as libc::uintptr_t,
        filter: libc::EVFILT_PROC,
        flags: libc::EV_ADD | libc::EV_CLEAR,
        fflags: libc::NOTE_EXIT,
        data: 0,
        udata: std::ptr::null_mut(),
    };
    loop {
        let result =
            unsafe { libc::kevent(kqueue, &event, 1, std::ptr::null_mut(), 0, std::ptr::null()) };
        if result >= 0 {
            return Ok(());
        }

        let error = std::io::Error::last_os_error();
        if error.kind() == std::io::ErrorKind::Interrupted {
            continue;
        }
        return if error.raw_os_error() == Some(libc::ESRCH) {
            Err(WatchProcessError::Gone)
        } else {
            Err(WatchProcessError::Other(error))
        };
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
