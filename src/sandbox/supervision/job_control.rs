//! Configures process groups and signal delivery for one sandbox job.
//!
//! Terminal-attached commands remain in the shell-created job process group so
//! ordinary pipeline and job-control semantics remain intact. Non-terminal
//! commands receive a dedicated process group for signal relay and teardown.

use std::os::unix::process::CommandExt;
use std::process::Command;

const FORWARDED_SIGNALS: [libc::c_int; 4] =
    [libc::SIGHUP, libc::SIGINT, libc::SIGQUIT, libc::SIGTERM];

#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum LaunchMode {
    TerminalAttached,
    Isolated,
}

impl LaunchMode {
    pub(super) fn detect() -> Self {
        if [libc::STDIN_FILENO, libc::STDOUT_FILENO, libc::STDERR_FILENO]
            .into_iter()
            .any(|descriptor| unsafe { libc::isatty(descriptor) } == 1)
        {
            Self::TerminalAttached
        } else {
            Self::Isolated
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
        let signal_set = handled_signal_set(launch_mode);
        let mut previous_mask: libc::sigset_t = unsafe { std::mem::zeroed() };
        let mask_result =
            unsafe { libc::pthread_sigmask(libc::SIG_BLOCK, &signal_set, &mut previous_mask) };
        if mask_result != 0 {
            return Err(format!(
                "failed to block sandbox-handled signals: {}",
                std::io::Error::from_raw_os_error(mask_result)
            ));
        }

        // Preserve inherited masks and ignored dispositions. Previously blocked
        // signals remain pending, while Darwin discards ignored signals. This
        // one-shot launcher keeps its new mask until it exits; the child restores
        // the inherited mask before exec.
        let mut wait_set: libc::sigset_t = unsafe { std::mem::zeroed() };
        unsafe { libc::sigemptyset(&mut wait_set) };
        for signal in handled_signals(launch_mode) {
            if unsafe { libc::sigismember(&previous_mask, signal) } == 0 {
                unsafe { libc::sigaddset(&mut wait_set, signal) };
            }
        }

        Ok(Self {
            wait_set,
            previous_mask,
            launch_mode,
        })
    }

    pub(super) fn configure_child(&self, command: &mut Command) {
        let previous_mask = unsafe { std::ptr::read(&self.previous_mask) };
        let isolate = self.launch_mode.is_isolated();

        unsafe {
            command.pre_exec(move || {
                if isolate && libc::setpgid(0, 0) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                let mask_result =
                    libc::pthread_sigmask(libc::SIG_SETMASK, &previous_mask, std::ptr::null_mut());
                if mask_result != 0 {
                    return Err(std::io::Error::from_raw_os_error(mask_result));
                }
                Ok(())
            });
        }
    }

    pub(super) fn relayed_signals(&self) -> impl Iterator<Item = libc::c_int> + '_ {
        handled_signals(self.launch_mode)
            .filter(|signal| unsafe { libc::sigismember(&self.wait_set, *signal) } == 1)
    }

    pub(super) fn handle_pending(&self, process_group: libc::pid_t) -> Result<(), String> {
        loop {
            let mut pending: libc::sigset_t = unsafe { std::mem::zeroed() };
            if unsafe { libc::sigpending(&mut pending) } != 0 {
                return Err(format!(
                    "failed to inspect pending launcher signals: {}",
                    std::io::Error::last_os_error()
                ));
            }
            if !handled_signals(self.launch_mode).any(|signal| {
                (unsafe { libc::sigismember(&self.wait_set, signal) } == 1)
                    && (unsafe { libc::sigismember(&pending, signal) } == 1)
            }) {
                return Ok(());
            }

            let mut signal = 0;
            let wait_result = unsafe { libc::sigwait(&self.wait_set, &mut signal) };
            if wait_result != 0 {
                return Err(format!(
                    "failed to consume a pending launcher signal: {}",
                    std::io::Error::from_raw_os_error(wait_result)
                ));
            }

            // A controlling terminal already delivered SIGHUP, SIGINT, and
            // SIGQUIT to every member of the shell-created job group, including
            // the sandbox root. Consume the launcher's copy instead of sending a
            // duplicate. An isolated command receives the signal only through
            // this relay, so forward it to the dedicated group.
            if self.launch_mode.is_isolated() {
                signal_process_group(process_group, signal)?;
            }
        }
    }
}

fn handled_signals(launch_mode: LaunchMode) -> impl Iterator<Item = libc::c_int> {
    FORWARDED_SIGNALS
        .into_iter()
        .filter(move |signal| launch_mode.is_isolated() || *signal != libc::SIGTERM)
}

fn handled_signal_set(launch_mode: LaunchMode) -> libc::sigset_t {
    let mut signal_set: libc::sigset_t = unsafe { std::mem::zeroed() };
    unsafe { libc::sigemptyset(&mut signal_set) };
    for signal in handled_signals(launch_mode) {
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
