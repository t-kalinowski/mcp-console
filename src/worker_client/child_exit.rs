use std::thread;
use std::time::{Duration, Instant};

const CHILD_EXIT_POLL_INTERVAL: Duration = Duration::from_millis(10);
const CHILD_EXITED: libc::c_int = 1;
const CHILD_KILLED: libc::c_int = 2;
const CHILD_DUMPED: libc::c_int = 3;

pub(super) struct ChildExitWaiter(libc::pid_t);

impl ChildExitWaiter {
    pub(super) fn start(process_id: u32) -> Result<Self, String> {
        libc::pid_t::try_from(process_id)
            .ok()
            .filter(|process_id| *process_id > 0)
            .map(Self)
            .ok_or_else(|| "child process ID is invalid".to_string())
    }

    pub(super) fn wait(&mut self, timeout: Duration) -> Result<bool, String> {
        let started = Instant::now();
        loop {
            if direct_child_has_exited(self.0)? {
                return Ok(true);
            }
            let remaining = timeout.saturating_sub(started.elapsed());
            if remaining.is_zero() {
                return Ok(false);
            }
            thread::sleep(remaining.min(CHILD_EXIT_POLL_INTERVAL));
        }
    }
}

fn direct_child_has_exited(process_id: libc::pid_t) -> Result<bool, String> {
    let wait_id = process_id as libc::id_t;
    loop {
        let mut information = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
        // SAFETY: `information` points to writable storage and `process_id`
        // identifies the direct child. WNOHANG makes observation bounded;
        // WNOWAIT keeps the child unreaped for its owner.
        let result = unsafe {
            libc::waitid(
                libc::P_PID,
                wait_id,
                information.as_mut_ptr(),
                libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
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

        // SAFETY: the structure was zero-initialized before `waitid`; on
        // success it is either populated or retains the no-event PID sentinel.
        let information = unsafe { information.assume_init() };
        if information.si_pid == 0 {
            return Ok(false);
        }
        if information.si_pid != process_id {
            return Err(format!(
                "waitid returned process {} while waiting for child process {process_id}",
                information.si_pid
            ));
        }
        return match information.si_code {
            CHILD_EXITED | CHILD_KILLED | CHILD_DUMPED => Ok(true),
            code => Err(format!(
                "waitid returned unexpected status code {code} for child process {process_id}"
            )),
        };
    }
}
