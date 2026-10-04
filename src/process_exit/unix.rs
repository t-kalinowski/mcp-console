use std::io;

const CHILD_EXITED: libc::c_int = 1;
const CHILD_KILLED: libc::c_int = 2;
const CHILD_DUMPED: libc::c_int = 3;
const CHILD_STOPPED: libc::c_int = 5;
const CHILD_CONTINUED: libc::c_int = 6;

pub(super) struct Observer(libc::pid_t);

impl Observer {
    pub(super) fn new(process_id: u32) -> Result<Self, String> {
        valid_process_id(process_id)
            .map(Self)
            .map_err(|_| "child process ID is invalid".to_string())
    }

    pub(super) fn wait(self) -> Result<(), String> {
        super::wait_for_direct_child_exit(self.0)
    }
}

pub(crate) fn direct_child_has_exited(process_id: u32) -> io::Result<bool> {
    observe_direct_child(valid_process_id(process_id)?, false)
}

pub(crate) fn wait_for_direct_child_exit(process_id: libc::pid_t) -> Result<(), String> {
    loop {
        match observe_direct_child(process_id, true) {
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

fn observe_direct_child(process_id: libc::pid_t, blocking: bool) -> io::Result<bool> {
    let wait_id = process_id as libc::id_t;
    let options = libc::WEXITED | libc::WNOWAIT | if blocking { 0 } else { libc::WNOHANG };

    loop {
        let mut information = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
        // SAFETY: `information` points to writable storage and `process_id`
        // identifies the direct child. WNOWAIT preserves its exit status for
        // the child owner, which remains the sole reaper.
        let result =
            unsafe { libc::waitid(libc::P_PID, wait_id, information.as_mut_ptr(), options) };
        if result < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error);
        }

        // SAFETY: successful `waitid` initialized the zeroed structure.
        let information = unsafe { information.assume_init() };
        // SAFETY: waitid populated the child-status variant of siginfo_t.
        let observed_pid = unsafe { information.si_pid() };
        if observed_pid == 0 {
            return Ok(false);
        }
        if observed_pid != process_id {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "waitid returned process {} while waiting for child process {process_id}",
                    observed_pid
                ),
            ));
        }
        match information.si_code {
            CHILD_EXITED | CHILD_KILLED | CHILD_DUMPED => return Ok(true),
            CHILD_STOPPED | CHILD_CONTINUED => {
                // Darwin can report these even for WEXITED. Consume only the
                // nonterminal notification and retry interrupted consumption.
                match consume_non_exit_notification(wait_id, process_id) {
                    Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                    result => result?,
                }
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
    // SAFETY: waitid populated the child-status variant of siginfo_t.
    let observed_pid = unsafe { information.si_pid() };
    if observed_pid == 0 {
        return Ok(());
    }
    if observed_pid != process_id {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "waitid returned process {} while consuming a notification for child process {process_id}",
                observed_pid
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
