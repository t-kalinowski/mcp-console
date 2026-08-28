//! Configures process groups and signal delivery for one foreground sandbox job.
//!
//! A controlling terminal sends signals directly to an exclusively owned child
//! group. When the launcher shares its foreground group with pipeline peers, it
//! retains terminal ownership and relays those signals to the child group.

use std::os::unix::process::CommandExt;
use std::process::Command;

const FORWARDED_SIGNALS: [libc::c_int; 4] =
    [libc::SIGHUP, libc::SIGINT, libc::SIGQUIT, libc::SIGTERM];

pub(super) struct ForegroundTerminal {
    descriptor: Option<libc::c_int>,
    launcher_process_group: libc::pid_t,
    transfer_to_child: bool,
}

impl ForegroundTerminal {
    pub(super) fn detect() -> Result<Self, String> {
        let launcher_process_group = unsafe { libc::getpgrp() };
        let launcher_process_id = unsafe { libc::getpid() };
        let descriptor = [libc::STDIN_FILENO, libc::STDOUT_FILENO, libc::STDERR_FILENO]
            .into_iter()
            .find(|descriptor| unsafe { libc::tcgetpgrp(*descriptor) } == launcher_process_group);
        let transfer_to_child = descriptor.is_some()
            && !process_group_has_peer(launcher_process_group, launcher_process_id)?;

        Ok(Self {
            descriptor,
            launcher_process_group,
            transfer_to_child,
        })
    }

    pub(super) fn descriptor(&self) -> Option<libc::c_int> {
        if self.transfer_to_child {
            self.descriptor
        } else {
            None
        }
    }

    pub(super) fn manages_job_control(&self) -> bool {
        self.descriptor.is_some()
    }

    pub(super) fn suspend(&mut self, child_process_group: libc::pid_t) -> Result<(), String> {
        let Some(descriptor) = self.descriptor else {
            return Ok(());
        };

        if self.transfer_to_child {
            set_foreground_process_group(descriptor, self.launcher_process_group).map_err(|error| {
                format!(
                    "failed to restore the launcher before stopping the sandbox job: {error}"
                )
            })?;
        }
        if unsafe { libc::kill(libc::getpid(), libc::SIGSTOP) } != 0 {
            return Err(format!(
                "failed to stop the sandbox launcher: {}",
                std::io::Error::last_os_error()
            ));
        }

        if self.transfer_to_child {
            let foreground = unsafe { libc::tcgetpgrp(descriptor) };
            if foreground == self.launcher_process_group {
                set_foreground_process_group(descriptor, child_process_group).map_err(|error| {
                    format!("failed to continue the sandbox job in the foreground: {error}")
                })?;
            } else if foreground < 0 {
                let error = std::io::Error::last_os_error();
                if error.raw_os_error() != Some(libc::ENOTTY) {
                    return Err(format!(
                        "failed to inspect terminal ownership after continuing the sandbox job: {error}"
                    ));
                }
                self.descriptor = None;
            } else {
                // `bg` leaves the shell in the foreground. Do not steal its
                // terminal later when this background job exits.
                self.descriptor = None;
            }
        }
        signal_process_group(child_process_group, libc::SIGCONT)
    }

    pub(super) fn restore(&mut self) -> Result<(), String> {
        let Some(descriptor) = self.descriptor else {
            return Ok(());
        };

        if self.transfer_to_child
            && let Err(error) =
                set_foreground_process_group(descriptor, self.launcher_process_group)
            && error.raw_os_error() != Some(libc::ENOTTY)
        {
            return Err(format!(
                "failed to restore the launcher as the foreground process group: {error}"
            ));
        }
        self.descriptor = None;
        Ok(())
    }
}

impl Drop for ForegroundTerminal {
    fn drop(&mut self) {
        let _ = self.restore();
    }
}

fn process_group_has_peer(
    process_group: libc::pid_t,
    process_id: libc::pid_t,
) -> Result<bool, String> {
    let mut capacity = 16;
    loop {
        let mut processes = vec![0; capacity];
        unsafe { *libc::__error() = 0 };
        let count = unsafe {
            libc::proc_listpgrppids(
                process_group,
                processes.as_mut_ptr().cast(),
                std::mem::size_of_val(processes.as_slice()) as libc::c_int,
            )
        };
        if count == 0 {
            let error_code = unsafe { *libc::__error() };
            if error_code == 0 || error_code == libc::ESRCH {
                return Ok(false);
            }
            if error_code == libc::EINTR {
                continue;
            }
            return Err(format!(
                "failed to inspect the foreground process group: {}",
                std::io::Error::from_raw_os_error(error_code)
            ));
        }
        if count < 0 {
            return Err(format!(
                "failed to inspect the foreground process group: \
                 proc_listpgrppids returned {count}"
            ));
        }

        let count = count as usize;
        if count < capacity {
            processes.truncate(count);
            return Ok(processes
                .into_iter()
                .any(|candidate| candidate > 0 && candidate != process_id));
        }
        capacity = capacity.saturating_mul(2).max(count + 16);
    }
}

fn set_foreground_process_group(
    descriptor: libc::c_int,
    process_group: libc::pid_t,
) -> std::io::Result<()> {
    let mut signal_set: libc::sigset_t = unsafe { std::mem::zeroed() };
    let mut previous_mask: libc::sigset_t = unsafe { std::mem::zeroed() };
    unsafe {
        libc::sigemptyset(&mut signal_set);
        libc::sigaddset(&mut signal_set, libc::SIGTTOU);
    }
    let mask_result =
        unsafe { libc::pthread_sigmask(libc::SIG_BLOCK, &signal_set, &mut previous_mask) };
    if mask_result != 0 {
        return Err(std::io::Error::from_raw_os_error(mask_result));
    }

    let terminal_result = unsafe { libc::tcsetpgrp(descriptor, process_group) };
    let terminal_error = std::io::Error::last_os_error();
    let mask_result =
        unsafe { libc::pthread_sigmask(libc::SIG_SETMASK, &previous_mask, std::ptr::null_mut()) };

    if terminal_result != 0 {
        return Err(terminal_error);
    }
    if mask_result != 0 {
        return Err(std::io::Error::from_raw_os_error(mask_result));
    }
    Ok(())
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

pub(super) struct SignalRelay {
    wait_set: libc::sigset_t,
    previous_mask: libc::sigset_t,
    forward_job_control: bool,
}

impl SignalRelay {
    pub(super) fn install(forward_job_control: bool) -> Result<Self, String> {
        let signal_set = forwarded_signal_set(forward_job_control);
        let mut previous_mask: libc::sigset_t = unsafe { std::mem::zeroed() };
        let mask_result =
            unsafe { libc::pthread_sigmask(libc::SIG_BLOCK, &signal_set, &mut previous_mask) };
        if mask_result != 0 {
            return Err(format!(
                "failed to block sandbox-forwarded signals: {}",
                std::io::Error::from_raw_os_error(mask_result)
            ));
        }

        // Preserve inherited masks and ignored dispositions. Previously blocked
        // signals remain pending, while Darwin discards ignored signals. This
        // one-shot launcher keeps its new mask until it exits; the child restores
        // the inherited mask before exec.
        let mut wait_set: libc::sigset_t = unsafe { std::mem::zeroed() };
        unsafe { libc::sigemptyset(&mut wait_set) };
        for signal in configured_signals(forward_job_control) {
            if unsafe { libc::sigismember(&previous_mask, signal) } == 0 {
                unsafe { libc::sigaddset(&mut wait_set, signal) };
            }
        }

        Ok(Self {
            wait_set,
            previous_mask,
            forward_job_control,
        })
    }

    pub(super) fn configure_child(
        &self,
        command: &mut Command,
        terminal_descriptor: Option<libc::c_int>,
    ) {
        let previous_mask = unsafe { std::ptr::read(&self.previous_mask) };

        unsafe {
            command.pre_exec(move || {
                if libc::setpgid(0, 0) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if let Some(descriptor) = terminal_descriptor {
                    set_foreground_process_group(descriptor, libc::getpid())?;
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
        configured_signals(self.forward_job_control)
            .filter(|signal| unsafe { libc::sigismember(&self.wait_set, *signal) } == 1)
    }

    pub(super) fn relay_pending(&self, process_group: libc::pid_t) -> Result<(), String> {
        loop {
            let mut pending: libc::sigset_t = unsafe { std::mem::zeroed() };
            if unsafe { libc::sigpending(&mut pending) } != 0 {
                return Err(format!(
                    "failed to inspect pending launcher signals: {}",
                    std::io::Error::last_os_error()
                ));
            }
            if !configured_signals(self.forward_job_control).any(|signal| {
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
            signal_process_group(process_group, signal)?;
        }
    }
}

fn configured_signals(forward_job_control: bool) -> impl Iterator<Item = libc::c_int> {
    FORWARDED_SIGNALS
        .into_iter()
        .chain(forward_job_control.then_some(libc::SIGTSTP))
}

fn forwarded_signal_set(forward_job_control: bool) -> libc::sigset_t {
    let mut signal_set: libc::sigset_t = unsafe { std::mem::zeroed() };
    unsafe { libc::sigemptyset(&mut signal_set) };
    for signal in configured_signals(forward_job_control) {
        unsafe { libc::sigaddset(&mut signal_set, signal) };
    }
    signal_set
}
