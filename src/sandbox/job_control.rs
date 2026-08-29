//! Launcher-directed signal forwarding.

const FORWARDED_SIGNALS: [libc::c_int; 4] =
    [libc::SIGHUP, libc::SIGINT, libc::SIGQUIT, libc::SIGTERM];

pub(super) struct SignalRelay {
    wait_set: libc::sigset_t,
    previous_mask: libc::sigset_t,
}

impl SignalRelay {
    pub(super) fn install() -> Result<Self, String> {
        let signal_set = forwarded_signal_set();
        let mut previous_mask: libc::sigset_t = unsafe { std::mem::zeroed() };
        let result =
            unsafe { libc::pthread_sigmask(libc::SIG_BLOCK, &signal_set, &mut previous_mask) };
        if result != 0 {
            return Err(format!(
                "failed to block sandbox-forwarded signals: {}",
                std::io::Error::from_raw_os_error(result)
            ));
        }
        let mut wait_set: libc::sigset_t = unsafe { std::mem::zeroed() };
        unsafe { libc::sigemptyset(&mut wait_set) };
        for signal in FORWARDED_SIGNALS {
            if unsafe { libc::sigismember(&previous_mask, signal) } == 0 {
                unsafe { libc::sigaddset(&mut wait_set, signal) };
            }
        }
        Ok(Self {
            wait_set,
            previous_mask,
        })
    }

    pub(super) fn inherited_mask(&self) -> libc::sigset_t {
        unsafe { std::ptr::read(&self.previous_mask) }
    }

    pub(super) fn pending(&self) -> Result<Vec<libc::c_int>, String> {
        let mut signals = Vec::new();
        loop {
            let mut pending: libc::sigset_t = unsafe { std::mem::zeroed() };
            if unsafe { libc::sigpending(&mut pending) } != 0 {
                return Err(format!(
                    "failed to inspect pending launcher signals: {}",
                    std::io::Error::last_os_error()
                ));
            }
            let Some(_) = FORWARDED_SIGNALS.iter().find(|signal| {
                (unsafe { libc::sigismember(&self.wait_set, **signal) } == 1)
                    && (unsafe { libc::sigismember(&pending, **signal) } == 1)
            }) else {
                return Ok(signals);
            };
            let mut signal = 0;
            let result = unsafe { libc::sigwait(&self.wait_set, &mut signal) };
            if result != 0 {
                return Err(format!(
                    "failed to consume a pending launcher signal: {}",
                    std::io::Error::from_raw_os_error(result)
                ));
            }
            signals.push(signal);
        }
    }
}

impl Drop for SignalRelay {
    fn drop(&mut self) {
        unsafe {
            libc::pthread_sigmask(libc::SIG_SETMASK, &self.previous_mask, std::ptr::null_mut());
        }
    }
}

fn forwarded_signal_set() -> libc::sigset_t {
    let mut signal_set: libc::sigset_t = unsafe { std::mem::zeroed() };
    unsafe { libc::sigemptyset(&mut signal_set) };
    for signal in FORWARDED_SIGNALS {
        unsafe { libc::sigaddset(&mut signal_set, signal) };
    }
    signal_set
}
