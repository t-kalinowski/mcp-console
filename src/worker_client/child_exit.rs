use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread;
use std::time::Duration;

const CHILD_EXITED: libc::c_int = 1;
const CHILD_KILLED: libc::c_int = 2;
const CHILD_DUMPED: libc::c_int = 3;
const CHILD_STOPPED: libc::c_int = 5;
const CHILD_CONTINUED: libc::c_int = 6;

pub(super) struct ChildExitWaiter {
    completion: Receiver<Result<(), String>>,
    result: Option<Result<(), String>>,
}

impl ChildExitWaiter {
    pub(super) fn start(process_id: u32) -> Result<Self, String> {
        let process_id = libc::pid_t::try_from(process_id)
            .ok()
            .filter(|process_id| *process_id > 0)
            .ok_or_else(|| "child process ID is invalid".to_string())?;
        let (sender, completion) = mpsc::sync_channel(1);
        thread::Builder::new()
            .name("worker launcher exit".to_string())
            .spawn(move || {
                let _ = sender.send(wait_for_direct_child_exit(process_id));
            })
            .map_err(|error| format!("failed to start child exit observer: {error}"))?;
        Ok(Self {
            completion,
            result: None,
        })
    }

    pub(super) fn wait(&mut self, timeout: Duration) -> Result<bool, String> {
        if let Some(result) = self.result.as_ref() {
            return result.clone().map(|()| true);
        }
        let result = match self.completion.recv_timeout(timeout) {
            Ok(result) => result,
            Err(RecvTimeoutError::Timeout) => return Ok(false),
            Err(RecvTimeoutError::Disconnected) => {
                Err("child exit observer stopped without a result".to_string())
            }
        };
        self.result = Some(result.clone());
        result.map(|()| true)
    }
}

fn wait_for_direct_child_exit(process_id: libc::pid_t) -> Result<(), String> {
    let wait_id = process_id as libc::id_t;
    loop {
        let mut information = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
        // SAFETY: `information` points to writable storage and `process_id`
        // identifies the direct child. WNOWAIT preserves its exit status for
        // the child owner, which remains the sole reaper.
        let result = unsafe {
            libc::waitid(
                libc::P_PID,
                wait_id,
                information.as_mut_ptr(),
                libc::WEXITED | libc::WNOWAIT,
            )
        };
        if result < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(format!(
                "failed to observe child process {process_id} exit: {error}"
            ));
        }

        // SAFETY: successful `waitid` initialized the zeroed structure.
        let information = unsafe { information.assume_init() };
        if information.si_pid != process_id {
            return Err(format!(
                "waitid returned process {} while waiting for child process {process_id}",
                information.si_pid
            ));
        }
        match information.si_code {
            CHILD_EXITED | CHILD_KILLED | CHILD_DUMPED => return Ok(()),
            CHILD_STOPPED | CHILD_CONTINUED => {
                consume_non_exit_notification(wait_id, process_id)?;
            }
            code => {
                return Err(format!(
                    "waitid returned unexpected status code {code} for child process {process_id}"
                ));
            }
        }
    }
}

fn consume_non_exit_notification(
    wait_id: libc::id_t,
    process_id: libc::pid_t,
) -> Result<(), String> {
    let mut information = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
    // SAFETY: `information` points to writable storage. Omitting WEXITED and
    // WNOWAIT consumes only a pending stop or continue notification.
    let result = unsafe {
        libc::waitid(
            libc::P_PID,
            wait_id,
            information.as_mut_ptr(),
            libc::WSTOPPED | libc::WCONTINUED | libc::WNOHANG,
        )
    };
    if result < 0 {
        return Err(format!(
            "failed to consume child process {process_id} status notification: {}",
            std::io::Error::last_os_error()
        ));
    }

    // SAFETY: successful `waitid` initialized the zeroed structure.
    let information = unsafe { information.assume_init() };
    if information.si_pid == 0 {
        return Ok(());
    }
    if information.si_pid != process_id {
        return Err(format!(
            "waitid returned process {} while consuming a notification for child process {process_id}",
            information.si_pid
        ));
    }
    if !matches!(information.si_code, CHILD_STOPPED | CHILD_CONTINUED) {
        return Err(format!(
            "waitid consumed unexpected status code {} for child process {process_id}",
            information.si_code
        ));
    }
    Ok(())
}
