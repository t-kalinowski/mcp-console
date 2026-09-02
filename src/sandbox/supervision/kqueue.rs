use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::sync::{Arc, Weak};
use std::time::Duration;

const EVENT_CAPACITY: usize = 32;

#[derive(Clone)]
pub(super) struct Kqueue {
    descriptor: Arc<OwnedFd>,
}

pub(super) struct WeakKqueue {
    descriptor: Weak<OwnedFd>,
}

pub(super) enum KqueueWait {
    Events(Vec<libc::kevent>),
    Interrupted,
    TimedOut,
}

pub(super) enum ProcessWatchError {
    Gone,
    Other(std::io::Error),
}

impl Kqueue {
    pub(super) fn new(description: &str) -> Result<Self, String> {
        let descriptor = unsafe { libc::kqueue() };
        if descriptor < 0 {
            return Err(format!(
                "failed to create {description}: {}",
                std::io::Error::last_os_error()
            ));
        }
        Ok(Self {
            descriptor: Arc::new(unsafe { OwnedFd::from_raw_fd(descriptor) }),
        })
    }

    pub(super) fn downgrade(&self) -> WeakKqueue {
        WeakKqueue {
            descriptor: Arc::downgrade(&self.descriptor),
        }
    }

    pub(super) fn watch_process(
        &self,
        pid: libc::pid_t,
        flags: u32,
    ) -> Result<(), ProcessWatchError> {
        let event = process_event(pid, libc::EV_ADD | libc::EV_CLEAR, flags);
        match submit_events(self.descriptor.as_raw_fd(), std::slice::from_ref(&event)) {
            Ok(()) => Ok(()),
            Err(error) if error.raw_os_error() == Some(libc::ESRCH) => Err(ProcessWatchError::Gone),
            Err(error) => Err(ProcessWatchError::Other(error)),
        }
    }

    pub(super) fn remove_process_watch(&self, pid: libc::pid_t) {
        let event = process_event(pid, libc::EV_DELETE, 0);
        let _ = submit_events(self.descriptor.as_raw_fd(), std::slice::from_ref(&event));
    }

    pub(super) fn watch_user(
        &self,
        ident: libc::uintptr_t,
        description: &str,
    ) -> Result<(), String> {
        let event = user_event(ident, libc::EV_ADD | libc::EV_CLEAR, 0);
        self.submit(std::slice::from_ref(&event), description)
    }

    pub(super) fn trigger_user(
        &self,
        ident: libc::uintptr_t,
        description: &str,
    ) -> Result<(), String> {
        let event = user_event(ident, 0, libc::NOTE_TRIGGER);
        self.submit(std::slice::from_ref(&event), description)
    }

    pub(super) fn watch_signals(
        &self,
        signals: impl IntoIterator<Item = libc::c_int>,
        description: &str,
    ) -> Result<(), String> {
        let events: Vec<_> = signals
            .into_iter()
            .map(|signal| libc::kevent {
                ident: signal as libc::uintptr_t,
                filter: libc::EVFILT_SIGNAL,
                flags: libc::EV_ADD | libc::EV_CLEAR,
                fflags: 0,
                data: 0,
                udata: std::ptr::null_mut(),
            })
            .collect();
        self.submit(&events, description)
    }

    pub(super) fn wait(
        &self,
        timeout: Option<Duration>,
        description: &str,
    ) -> Result<KqueueWait, String> {
        let mut events: [libc::kevent; EVENT_CAPACITY] =
            unsafe { std::mem::MaybeUninit::zeroed().assume_init() };
        let timeout = timeout.map(|duration| libc::timespec {
            tv_sec: duration.as_secs() as libc::time_t,
            tv_nsec: duration.subsec_nanos() as libc::c_long,
        });
        let timeout = timeout
            .as_ref()
            .map_or(std::ptr::null(), |timeout| timeout as *const _);

        let count = unsafe {
            libc::kevent(
                self.descriptor.as_raw_fd(),
                std::ptr::null(),
                0,
                events.as_mut_ptr(),
                events.len() as libc::c_int,
                timeout,
            )
        };
        if count < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                return Ok(KqueueWait::Interrupted);
            }
            return Err(format!("{description}: {error}"));
        }
        if count == 0 {
            return Ok(KqueueWait::TimedOut);
        }
        Ok(KqueueWait::Events(
            events.iter().take(count as usize).copied().collect(),
        ))
    }

    fn submit(&self, events: &[libc::kevent], description: &str) -> Result<(), String> {
        submit_events(self.descriptor.as_raw_fd(), events)
            .map_err(|error| format!("{description}: {error}"))
    }
}

impl WeakKqueue {
    pub(super) fn trigger_user(
        self,
        ident: libc::uintptr_t,
        description: &str,
    ) -> Result<(), String> {
        let Some(descriptor) = self.descriptor.upgrade() else {
            return Ok(());
        };
        let event = user_event(ident, 0, libc::NOTE_TRIGGER);
        submit_events(descriptor.as_raw_fd(), std::slice::from_ref(&event))
            .map_err(|error| format!("{description}: {error}"))
    }
}

fn submit_events(descriptor: libc::c_int, events: &[libc::kevent]) -> std::io::Result<()> {
    if events.is_empty() {
        return Ok(());
    }
    loop {
        let result = unsafe {
            libc::kevent(
                descriptor,
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
            return Err(error);
        }
    }
}

fn process_event(pid: libc::pid_t, flags: u16, fflags: u32) -> libc::kevent {
    libc::kevent {
        ident: pid as libc::uintptr_t,
        filter: libc::EVFILT_PROC,
        flags,
        fflags,
        data: 0,
        udata: std::ptr::null_mut(),
    }
}

fn user_event(ident: libc::uintptr_t, flags: u16, fflags: u32) -> libc::kevent {
    libc::kevent {
        ident,
        filter: libc::EVFILT_USER,
        flags,
        fflags,
        data: 0,
        udata: std::ptr::null_mut(),
    }
}
