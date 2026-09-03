//! Configures process groups and signal delivery for one foreground sandbox job.
//!
//! A controlling terminal, including a PTY slave, sends terminal-generated signals
//! directly to an exclusively owned child group. When the launcher shares its
//! foreground group with peers, it retains terminal ownership and relays signals
//! addressed to the launcher into the child group.

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

    pub(super) fn restore(&mut self) -> Result<(), String> {
        let Some(descriptor) = self.descriptor else {
            return Ok(());
        };

        // A revoked or hung-up controlling terminal no longer has foreground
        // ownership to restore. Preserve the command's actual exit status.
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

impl Drop for ForegroundTerminal {
    fn drop(&mut self) {
        let _ = self.restore();
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

pub(in crate::sandbox) struct SignalRelay {
    wait_set: libc::sigset_t,
    previous_mask: libc::sigset_t,
    previous_sigterm_action: Option<libc::sigaction>,
    restore_pending: bool,
}

impl SignalRelay {
    pub(super) fn install(retire_on_sigterm: bool) -> Result<Self, String> {
        let signal_set = forwarded_signal_set();
        let mut previous_mask: libc::sigset_t = unsafe { std::mem::zeroed() };
        let mask_result =
            unsafe { libc::pthread_sigmask(libc::SIG_BLOCK, &signal_set, &mut previous_mask) };
        if mask_result != 0 {
            return Err(format!(
                "failed to block sandbox-forwarded signals: {}",
                std::io::Error::from_raw_os_error(mask_result)
            ));
        }

        let previous_sigterm_action = if retire_on_sigterm {
            match reset_ignored_sigterm() {
                Ok(action) => action,
                Err(error) => {
                    return Err(match restore_signal_mask(&previous_mask) {
                        Ok(()) => error,
                        Err(restore_error) => format!(
                            "{error}; additionally, failed to restore the launcher signal mask: \
                             {restore_error}"
                        ),
                    });
                }
            }
        } else {
            None
        };

        // Preserve inherited masks and dispositions. Owned mode temporarily
        // resets an ignored SIGTERM so Darwin will retain the blocked retirement
        // request. Spawned children restore the inherited state before exec, and
        // the launcher restores it after the sandbox root exits.
        let mut wait_set: libc::sigset_t = unsafe { std::mem::zeroed() };
        unsafe { libc::sigemptyset(&mut wait_set) };
        for signal in FORWARDED_SIGNALS {
            if retire_on_sigterm && signal == libc::SIGTERM
                || unsafe { libc::sigismember(&previous_mask, signal) } == 0
            {
                unsafe { libc::sigaddset(&mut wait_set, signal) };
            }
        }

        Ok(Self {
            wait_set,
            previous_mask,
            previous_sigterm_action,
            restore_pending: true,
        })
    }

    pub(in crate::sandbox) fn configure_child(
        &self,
        command: &mut Command,
        terminal_descriptor: Option<libc::c_int>,
    ) {
        let previous_mask = unsafe { std::ptr::read(&self.previous_mask) };
        let previous_sigterm_action = self
            .previous_sigterm_action
            .as_ref()
            .map(|action| unsafe { std::ptr::read(action) });

        unsafe {
            command.pre_exec(move || {
                // Give the command a dedicated process group. If this launcher
                // owns a terminal, hand foreground control to that group before
                // exec so terminal signals reach it directly and exactly once.
                // Stopped/continued job-control state is intentionally not
                // proxied; supporting Ctrl-Z requires a separate wait state
                // machine that restores and later reassigns the terminal.
                if libc::setpgid(0, 0) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if let Some(descriptor) = terminal_descriptor {
                    set_foreground_process_group(descriptor, libc::getpid())?;
                }
                restore_signal_action(libc::SIGTERM, previous_sigterm_action.as_ref())?;
                restore_signal_mask(&previous_mask)
            });
        }
    }

    pub(super) fn configure_manager(&self, command: &mut Command) {
        let previous_mask = unsafe { std::ptr::read(&self.previous_mask) };
        let previous_sigterm_action = self
            .previous_sigterm_action
            .as_ref()
            .map(|action| unsafe { std::ptr::read(action) });

        unsafe {
            command.pre_exec(move || {
                restore_signal_action(libc::SIGTERM, previous_sigterm_action.as_ref())?;
                restore_signal_mask(&previous_mask)
            });
        }
    }

    pub(super) fn drain_pending_and_restore(mut self) -> Result<(), String> {
        self.drain_pending()?;
        self.restore_sigterm_action()?;
        self.restore_mask()
    }

    fn restore_sigterm_action(&mut self) -> Result<(), String> {
        let Some(action) = self.previous_sigterm_action.as_ref() else {
            return Ok(());
        };
        restore_signal_action(libc::SIGTERM, Some(action)).map_err(|error| {
            format!("failed to restore the launcher SIGTERM disposition: {error}")
        })?;
        self.previous_sigterm_action = None;
        Ok(())
    }

    fn restore_mask(&mut self) -> Result<(), String> {
        if !self.restore_pending {
            return Ok(());
        }
        let mask_result = unsafe {
            libc::pthread_sigmask(libc::SIG_SETMASK, &self.previous_mask, std::ptr::null_mut())
        };
        if mask_result != 0 {
            return Err(format!(
                "failed to restore the launcher signal mask: {}",
                std::io::Error::from_raw_os_error(mask_result)
            ));
        }
        self.restore_pending = false;
        Ok(())
    }

    pub(super) fn relayed_signals(&self) -> impl Iterator<Item = libc::c_int> + '_ {
        FORWARDED_SIGNALS
            .into_iter()
            .filter(|signal| unsafe { libc::sigismember(&self.wait_set, *signal) } == 1)
    }

    pub(super) fn relay_pending(
        &self,
        process_group: libc::pid_t,
        retire_on_sigterm: bool,
    ) -> Result<bool, String> {
        while let Some(signal) = self.take_pending()? {
            if retire_on_sigterm && signal == libc::SIGTERM {
                return Ok(true);
            }
            let result = unsafe { libc::kill(-process_group, signal) };
            if result != 0 {
                let error = std::io::Error::last_os_error();
                if error.raw_os_error() != Some(libc::ESRCH) {
                    return Err(format!(
                        "failed to relay signal {signal} to the sandbox process group: {error}"
                    ));
                }
            }
        }
        Ok(false)
    }

    fn drain_pending(&self) -> Result<(), String> {
        while self.take_pending()?.is_some() {}
        Ok(())
    }

    fn take_pending(&self) -> Result<Option<libc::c_int>, String> {
        let mut pending: libc::sigset_t = unsafe { std::mem::zeroed() };
        if unsafe { libc::sigpending(&mut pending) } != 0 {
            return Err(format!(
                "failed to inspect pending launcher signals: {}",
                std::io::Error::last_os_error()
            ));
        }
        if !FORWARDED_SIGNALS.iter().any(|signal| {
            (unsafe { libc::sigismember(&self.wait_set, *signal) } == 1)
                && (unsafe { libc::sigismember(&pending, *signal) } == 1)
        }) {
            return Ok(None);
        }

        let mut signal = 0;
        let wait_result = unsafe { libc::sigwait(&self.wait_set, &mut signal) };
        if wait_result != 0 {
            return Err(format!(
                "failed to consume a pending launcher signal: {}",
                std::io::Error::from_raw_os_error(wait_result)
            ));
        }
        Ok(Some(signal))
    }
}

fn restore_signal_mask(mask: &libc::sigset_t) -> std::io::Result<()> {
    let result = unsafe { libc::pthread_sigmask(libc::SIG_SETMASK, mask, std::ptr::null_mut()) };
    if result != 0 {
        return Err(std::io::Error::from_raw_os_error(result));
    }
    Ok(())
}

fn reset_ignored_sigterm() -> Result<Option<libc::sigaction>, String> {
    let mut inherited: libc::sigaction = unsafe { std::mem::zeroed() };
    if unsafe { libc::sigaction(libc::SIGTERM, std::ptr::null(), &mut inherited) } != 0 {
        return Err(format!(
            "failed to inspect the launcher SIGTERM disposition: {}",
            std::io::Error::last_os_error()
        ));
    }
    if inherited.sa_sigaction != libc::SIG_IGN {
        return Ok(None);
    }

    let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
    unsafe { libc::sigemptyset(&mut action.sa_mask) };
    action.sa_sigaction = libc::SIG_DFL;
    restore_signal_action(libc::SIGTERM, Some(&action))
        .map_err(|error| format!("failed to reset the launcher SIGTERM disposition: {error}"))?;
    Ok(Some(inherited))
}

fn restore_signal_action(
    signal: libc::c_int,
    action: Option<&libc::sigaction>,
) -> std::io::Result<()> {
    let Some(action) = action else {
        return Ok(());
    };
    if unsafe { libc::sigaction(signal, action, std::ptr::null_mut()) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

impl Drop for SignalRelay {
    fn drop(&mut self) {
        if self.restore_pending
            && self.drain_pending().is_ok()
            && self.restore_sigterm_action().is_ok()
        {
            let _ = self.restore_mask();
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
