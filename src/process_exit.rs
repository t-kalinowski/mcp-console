//! Direct-child exit observation shared by ordinary process owners.

use std::io;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread;
use std::time::{Duration, Instant};

const CHILD_EXITED: libc::c_int = 1;
const CHILD_KILLED: libc::c_int = 2;
const CHILD_DUMPED: libc::c_int = 3;
const CHILD_STOPPED: libc::c_int = 5;
const CHILD_CONTINUED: libc::c_int = 6;
const CHILD_EXIT_POLL_INTERVAL: Duration = Duration::from_millis(1);

pub(crate) struct ChildExitWaiter {
    completion: Receiver<Result<(), String>>,
    result: Option<Result<(), String>>,
}

impl ChildExitWaiter {
    pub(crate) fn start(process_id: u32) -> Result<Self, String> {
        let process_id =
            valid_process_id(process_id).map_err(|_| "child process ID is invalid".to_string())?;
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

    pub(crate) fn wait(&mut self, timeout: Duration) -> Result<bool, String> {
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

pub(crate) fn wait_for_process_exit_without_reaping(
    process_id: u32,
    timeout: Duration,
) -> io::Result<bool> {
    let process_id = valid_process_id(process_id)?;
    let deadline = Instant::now() + timeout;

    loop {
        match observe_direct_child(process_id, true) {
            Ok(true) => return Ok(true),
            Ok(false) => {}
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {
                if Instant::now() >= deadline {
                    return Ok(false);
                }
                continue;
            }
            Err(error) => return Err(error),
        }

        let now = Instant::now();
        if now >= deadline {
            return Ok(false);
        }
        thread::sleep(CHILD_EXIT_POLL_INTERVAL.min(deadline.saturating_duration_since(now)));
    }
}

fn wait_for_direct_child_exit(process_id: libc::pid_t) -> Result<(), String> {
    loop {
        match observe_direct_child(process_id, false) {
            Ok(true) => return Ok(()),
            Ok(false) => {}
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => {
                return Err(format!(
                    "failed to observe child process {process_id} exit: {error}"
                ));
            }
        }
    }
}

fn observe_direct_child(process_id: libc::pid_t, nonblocking: bool) -> io::Result<bool> {
    let wait_id = process_id as libc::id_t;
    let mut options = libc::WEXITED | libc::WNOWAIT;
    if nonblocking {
        options |= libc::WNOHANG;
    }

    loop {
        let mut information = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
        // SAFETY: `information` points to writable storage and `process_id`
        // identifies the direct child. WNOWAIT preserves its exit status for
        // the child owner, which remains the sole reaper.
        let result =
            unsafe { libc::waitid(libc::P_PID, wait_id, information.as_mut_ptr(), options) };
        if result < 0 {
            return Err(io::Error::last_os_error());
        }

        // SAFETY: successful `waitid` initialized the zeroed structure. Darwin
        // leaves `si_pid` zero when WNOHANG finds no matching event.
        let information = unsafe { information.assume_init() };
        if information.si_pid == 0 {
            return Ok(false);
        }
        if information.si_pid != process_id {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "waitid returned process {} while waiting for child process {process_id}",
                    information.si_pid
                ),
            ));
        }
        match information.si_code {
            CHILD_EXITED | CHILD_KILLED | CHILD_DUMPED => return Ok(true),
            CHILD_STOPPED | CHILD_CONTINUED => {
                consume_non_exit_notification(wait_id, process_id)?;
            }
            code => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!(
                        "waitid returned unexpected status code {code} for child process {process_id}"
                    ),
                ));
            }
        }
    }
}

fn consume_non_exit_notification(wait_id: libc::id_t, process_id: libc::pid_t) -> io::Result<()> {
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
        return Err(io::Error::last_os_error());
    }

    // SAFETY: successful `waitid` initialized the zeroed structure.
    let information = unsafe { information.assume_init() };
    if information.si_pid == 0 {
        return Ok(());
    }
    if information.si_pid != process_id {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "waitid returned process {} while consuming a notification for child process {process_id}",
                information.si_pid
            ),
        ));
    }
    if !matches!(information.si_code, CHILD_STOPPED | CHILD_CONTINUED) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "waitid consumed unexpected status code {} for child process {process_id}",
                information.si_code
            ),
        ));
    }
    Ok(())
}

fn valid_process_id(process_id: u32) -> io::Result<libc::pid_t> {
    libc::pid_t::try_from(process_id)
        .ok()
        .filter(|process_id| *process_id > 0)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid process ID"))
}
