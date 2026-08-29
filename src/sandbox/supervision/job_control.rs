use std::os::unix::process::CommandExt;
use std::process::Command;
use std::sync::atomic::{AtomicI32, Ordering};

const FORWARDED_SIGNALS: [libc::c_int; 4] =
    [libc::SIGHUP, libc::SIGINT, libc::SIGQUIT, libc::SIGTERM];
static TERMINAL_ROOT: AtomicI32 = AtomicI32::new(0);

#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum LaunchMode {
    TerminalAttached,
    Isolated,
}

impl LaunchMode {
    pub(super) fn detect() -> Self {
        let terminal =
            unsafe { libc::open(c"/dev/tty".as_ptr(), libc::O_RDONLY | libc::O_CLOEXEC) };
        if terminal < 0 {
            Self::Isolated
        } else {
            unsafe { libc::close(terminal) };
            Self::TerminalAttached
        }
    }

    pub(super) fn is_isolated(self) -> bool {
        self == Self::Isolated
    }
}

pub(super) struct SignalRelay {
    wait_set: libc::sigset_t,
    previous_mask: libc::sigset_t,
    launch_mode: LaunchMode,
}

impl SignalRelay {
    pub(super) fn install(launch_mode: LaunchMode) -> Result<Self, String> {
        let signal_set = signal_set(FORWARDED_SIGNALS);
        let mut previous_mask: libc::sigset_t = unsafe { std::mem::zeroed() };
        let result =
            unsafe { libc::pthread_sigmask(libc::SIG_BLOCK, &signal_set, &mut previous_mask) };
        if result != 0 {
            return Err(pthread_error("failed to block sandbox-handled signals", result));
        }

        let wait_set = signal_set(FORWARDED_SIGNALS.into_iter().filter(|signal| unsafe {
            libc::sigismember(&previous_mask, *signal) == 0
        }));
        Ok(Self {
            wait_set,
            previous_mask,
            launch_mode,
        })
    }

    fn activate(&self, root_pid: libc::pid_t) -> Result<(), String> {
        if self.launch_mode.is_isolated() {
            return Ok(());
        }
        TERMINAL_ROOT.store(root_pid, Ordering::Relaxed);
        for signal in FORWARDED_SIGNALS {
            if unsafe { libc::sigismember(&self.wait_set, signal) } == 1 {
                install_terminal_handler(signal)?;
            }
        }
        let result = unsafe {
            libc::pthread_sigmask(
                libc::SIG_SETMASK,
                &self.previous_mask,
                std::ptr::null_mut(),
            )
        };
        if result != 0 {
            return Err(pthread_error(
                "failed to activate terminal signal handling",
                result,
            ));
        }
        Ok(())
    }

    pub(super) fn configure_child(&self, command: &mut Command) {
        let previous_mask = unsafe { std::ptr::read(&self.previous_mask) };
        let isolate = self.launch_mode.is_isolated();
        unsafe {
            command.pre_exec(move || {
                if isolate && libc::setpgid(0, 0) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                let result =
                    libc::pthread_sigmask(libc::SIG_SETMASK, &previous_mask, std::ptr::null_mut());
                if result == 0 {
                    Ok(())
                } else {
                    Err(std::io::Error::from_raw_os_error(result))
                }
            });
        }
    }

    pub(super) fn relayed_signals(&self) -> impl Iterator<Item = libc::c_int> + '_ {
        FORWARDED_SIGNALS.into_iter().filter(|signal| {
            self.launch_mode.is_isolated()
                && unsafe { libc::sigismember(&self.wait_set, *signal) } == 1
        })
    }

    pub(super) fn handle_pending(&self, process_group: libc::pid_t) -> Result<(), String> {
        if !self.launch_mode.is_isolated() {
            if TERMINAL_ROOT.load(Ordering::Relaxed) == 0 {
                self.activate(process_group)?;
            }
            return Ok(());
        }
        loop {
            let mut pending: libc::sigset_t = unsafe { std::mem::zeroed() };
            if unsafe { libc::sigpending(&mut pending) } != 0 {
                return Err(os_error("failed to inspect pending launcher signals"));
            }
            if !FORWARDED_SIGNALS
                .into_iter()
                .any(|signal| unsafe {
                    libc::sigismember(&self.wait_set, signal) == 1
                        && libc::sigismember(&pending, signal) == 1
                })
            {
                return Ok(());
            }

            let mut signal = 0;
            let result = unsafe { libc::sigwait(&self.wait_set, &mut signal) };
            if result != 0 {
                return Err(format!(
                    "failed to consume a pending launcher signal: {}",
                    std::io::Error::from_raw_os_error(result)
                ));
            }
            signal_process_group(process_group, signal)?;
        }
    }
}

extern "C" fn relay_direct_signal(
    signal: libc::c_int,
    info: *mut libc::siginfo_t,
    _context: *mut libc::c_void,
) {
    if !info.is_null() && unsafe { (*info).si_pid } != 0 {
        let root_pid = TERMINAL_ROOT.load(Ordering::Relaxed);
        if root_pid > 0 {
            let errno = unsafe { *libc::__error() };
            unsafe {
                libc::kill(root_pid, signal);
                *libc::__error() = errno;
            }
        }
    }
}

fn install_terminal_handler(signal: libc::c_int) -> Result<(), String> {
    let mut previous: libc::sigaction = unsafe { std::mem::zeroed() };
    if unsafe { libc::sigaction(signal, std::ptr::null(), &mut previous) } != 0 {
        return Err(os_error("failed to inspect terminal signal disposition"));
    }
    if previous.sa_sigaction == libc::SIG_IGN {
        return Ok(());
    }
    let action = libc::sigaction {
        sa_sigaction: relay_direct_signal as libc::sighandler_t,
        sa_mask: signal_set([]),
        sa_flags: libc::SA_SIGINFO,
    };
    if unsafe { libc::sigaction(signal, &action, std::ptr::null_mut()) } != 0 {
        return Err(os_error("failed to install terminal signal handler"));
    }
    Ok(())
}

fn os_error(action: &str) -> String {
    format!("{action}: {}", std::io::Error::last_os_error())
}

fn pthread_error(action: &str, result: libc::c_int) -> String {
    format!("{action}: {}", std::io::Error::from_raw_os_error(result))
}

fn signal_set(signals: impl IntoIterator<Item = libc::c_int>) -> libc::sigset_t {
    let mut signal_set: libc::sigset_t = unsafe { std::mem::zeroed() };
    unsafe { libc::sigemptyset(&mut signal_set) };
    for signal in signals {
        unsafe { libc::sigaddset(&mut signal_set, signal) };
    }
    signal_set
}

fn signal_process_group(process_group: libc::pid_t, signal: libc::c_int) -> Result<(), String> {
    if unsafe { libc::kill(-process_group, signal) } == 0 {
        return Ok(());
    }
    let error = std::io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::ESRCH) {
        Ok(())
    } else {
        Err(format!(
            "failed to signal the sandbox process group with {signal}: {error}"
        ))
    }
}
