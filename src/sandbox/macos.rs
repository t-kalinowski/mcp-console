use std::ffi::OsString;
use std::fs::{self, DirBuilder};
use std::io;
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, ExitStatus};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub(super) const SANDBOX_EXEC: &str = "/usr/bin/sandbox-exec";
const POLICY: &str = include_str!("read_only_policy.sbpl");
const CHILD_EXITED: libc::c_int = 1;
const CHILD_KILLED: libc::c_int = 2;
const CHILD_DUMPED: libc::c_int = 3;
const CHILD_STOPPED: libc::c_int = 5;
const CHILD_CONTINUED: libc::c_int = 6;
const CHILD_EXIT_POLL_INTERVAL: Duration = Duration::from_millis(1);
const PROCESS_GROUP_STOP_POLL_INTERVAL: Duration = Duration::from_millis(1);
const PROCESS_GROUP_STOP_TIMEOUT: Duration = Duration::from_secs(1);

pub(super) fn wait_for_process_exit_without_reaping(
    process_id: u32,
    timeout: Duration,
) -> io::Result<bool> {
    let process_id = valid_process_id(process_id, "process")?;
    let wait_id = libc::id_t::try_from(process_id)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "invalid process ID"))?;
    let deadline = Instant::now() + timeout;

    loop {
        let mut information = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
        // SAFETY: `information` points to writable `siginfo_t` storage and the
        // positive PID names our direct child. `WNOWAIT` observes termination
        // without consuming the wait status, keeping the PID unavailable for
        // reuse until the caller finishes its cleanup and reaps it.
        let result = unsafe {
            libc::waitid(
                libc::P_PID,
                wait_id,
                information.as_mut_ptr(),
                libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
            )
        };
        if result < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                if Instant::now() >= deadline {
                    return Ok(false);
                }
                continue;
            }
            return Err(error);
        }

        // SAFETY: successful `waitid` initialized the supplied `siginfo_t`.
        // Darwin leaves `si_pid` zero when `WNOHANG` finds no matching event.
        let information = unsafe { information.assume_init() };
        let observed_process_id = information.si_pid;
        if observed_process_id == process_id {
            match information.si_code {
                CHILD_EXITED | CHILD_KILLED | CHILD_DUMPED => return Ok(true),
                CHILD_STOPPED | CHILD_CONTINUED => {
                    // Darwin may report a pending stop or continue notification
                    // even though this observation requested only `WEXITED`.
                    // Consume only non-exit notifications so a stopped child is
                    // not mistaken for an exited child, while leaving any exit
                    // status waitable to pin its identity.
                    if let Err(error) = consume_non_exit_notification(wait_id, process_id) {
                        if error.kind() == io::ErrorKind::Interrupted {
                            if Instant::now() >= deadline {
                                return Ok(false);
                            }
                            continue;
                        }
                        return Err(error);
                    }
                    continue;
                }
                code => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!("waitid returned unexpected child status code {code}"),
                    ));
                }
            }
        }
        if observed_process_id != 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "waitid returned process {observed_process_id} while waiting for {process_id}"
                ),
            ));
        }

        let now = Instant::now();
        if now >= deadline {
            return Ok(false);
        }
        std::thread::sleep(CHILD_EXIT_POLL_INTERVAL.min(deadline.saturating_duration_since(now)));
    }
}

pub(super) fn wait_for_process_exit_without_reaping_blocking(process_id: u32) -> io::Result<()> {
    let process_id = valid_process_id(process_id, "process")?;
    let wait_id = libc::id_t::try_from(process_id)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "invalid process ID"))?;

    loop {
        let mut information = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
        // SAFETY: `information` points to writable `siginfo_t` storage and the
        // positive PID names our direct child. `WNOWAIT` leaves the exit status
        // available for the monitor thread to reap after it inspects the result.
        let result = unsafe {
            libc::waitid(
                libc::P_PID,
                wait_id,
                information.as_mut_ptr(),
                libc::WEXITED | libc::WNOWAIT,
            )
        };
        if result < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error);
        }

        // SAFETY: successful `waitid` initialized the supplied `siginfo_t`.
        let information = unsafe { information.assume_init() };
        if information.si_pid != process_id {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "waitid returned process {} while waiting for {process_id}",
                    information.si_pid
                ),
            ));
        }
        match information.si_code {
            CHILD_EXITED | CHILD_KILLED | CHILD_DUMPED => return Ok(()),
            CHILD_STOPPED | CHILD_CONTINUED => {
                consume_non_exit_notification(wait_id, process_id)?;
            }
            code => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("waitid returned unexpected child status code {code}"),
                ));
            }
        }
    }
}

fn consume_non_exit_notification(wait_id: libc::id_t, process_id: libc::pid_t) -> io::Result<()> {
    let mut information = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
    // SAFETY: `information` points to writable `siginfo_t` storage and the
    // positive PID names our direct child. Omitting `WEXITED` and `WNOWAIT`
    // consumes only a pending stop or continue notification, never the exit
    // status that keeps the child's PID unavailable for reuse.
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

    // SAFETY: successful `waitid` initialized the supplied `siginfo_t`.
    let information = unsafe { information.assume_init() };
    if information.si_pid == 0 {
        return Ok(());
    }
    if information.si_pid != process_id {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "waitid returned process {} while consuming a notification for {process_id}",
                information.si_pid
            ),
        ));
    }
    if !matches!(information.si_code, CHILD_STOPPED | CHILD_CONTINUED) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "waitid consumed unexpected child status code {}",
                information.si_code
            ),
        ));
    }
    Ok(())
}

pub(super) fn kill_process_group(process_group_id: u32) -> io::Result<()> {
    let process_group_id = valid_process_id(process_group_id, "process group")?;

    // SAFETY: the group ID is positive and identifies the group created for
    // the sandbox child.
    if unsafe { libc::killpg(process_group_id, libc::SIGKILL) } < 0 {
        let group_error = io::Error::last_os_error();
        if group_error.raw_os_error() == Some(libc::ESRCH) {
            return Ok(());
        }
        if group_error.kind() != io::ErrorKind::PermissionDenied {
            return Err(group_error);
        }
    }

    kill_process_group_members(process_group_id, None)
}

pub(super) fn kill_process_group_members_except(
    process_group_id: u32,
    excluded_process_id: u32,
) -> io::Result<()> {
    let process_group_id = valid_process_id(process_group_id, "process group")?;
    let excluded_process_id = valid_process_id(excluded_process_id, "excluded process")?;
    kill_process_group_members(process_group_id, Some(excluded_process_id))
}

fn valid_process_id(process_id: u32, kind: &str) -> io::Result<libc::pid_t> {
    libc::pid_t::try_from(process_id)
        .ok()
        .filter(|process_id| *process_id > 0)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, format!("invalid {kind} ID")))
}

fn kill_process_group_members(
    process_group_id: libc::pid_t,
    excluded_process_id: Option<libc::pid_t>,
) -> io::Result<()> {
    // Group signalling and the EPERM fallback both operate on PID snapshots.
    // The full-group caller normally keeps the direct leader unreaped while
    // rescanning so its PID pins the process-group identity until the caller
    // collects it. If it was already reaped, exact membership checks still
    // prevent a stale snapshot from targeting a process that changed groups.
    // The relay-side caller remains alive as the excluded group leader.
    let deadline = Instant::now() + PROCESS_GROUP_STOP_TIMEOUT;
    loop {
        let mut members = process_group_members(process_group_id)?;
        if excluded_process_id.is_none() && !members.contains(&process_group_id) {
            members.push(process_group_id);
        }
        // Stop the leader before its descendants so it cannot fork after this
        // snapshot. A later pass must observe the exact group as quiescent.
        members.sort_unstable_by_key(|process_id| *process_id != process_group_id);

        let mut observed_live_member = false;
        let mut first_error = None;
        for process_id in members {
            if process_id <= 0 || Some(process_id) == excluded_process_id {
                continue;
            }
            match process_is_live_group_member(process_id, process_group_id) {
                Ok(false) => continue,
                Ok(true) => observed_live_member = true,
                Err(error) if first_error.is_none() => {
                    first_error = Some(error);
                    continue;
                }
                Err(_) => continue,
            }
            match process_group_of(process_id) {
                Ok(Some(current_group)) if current_group == process_group_id => {
                    if let Err(error) = kill_process(process_id)
                        && first_error.is_none()
                    {
                        first_error = Some(error);
                    }
                }
                Ok(Some(_)) | Ok(None) => {}
                Err(error) if first_error.is_none() => first_error = Some(error),
                Err(_) => {}
            }
        }

        if let Some(error) = first_error {
            return Err(error);
        }
        // Darwin keeps zombies in process-group snapshots. A later pass with
        // no live exact-group member is therefore the safe stopping condition.
        if !observed_live_member {
            return Ok(());
        }

        let now = Instant::now();
        if now >= deadline {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                format!(
                    "process group {process_group_id} remained live after {} ms",
                    PROCESS_GROUP_STOP_TIMEOUT.as_millis()
                ),
            ));
        }

        std::thread::sleep(
            PROCESS_GROUP_STOP_POLL_INTERVAL.min(deadline.saturating_duration_since(now)),
        );
    }
}

fn process_group_members(process_group_id: libc::pid_t) -> io::Result<Vec<libc::pid_t>> {
    let mut process_ids: Vec<libc::pid_t> = vec![0; 16];
    loop {
        let buffer_size = libc::c_int::try_from(std::mem::size_of_val(process_ids.as_slice()))
            .map_err(|_| {
                io::Error::new(io::ErrorKind::InvalidData, "process group is too large")
            })?;
        // SAFETY: the buffer is writable for `buffer_size` bytes, and the
        // positive group ID identifies the group created for the child.
        clear_errno();
        let count = unsafe {
            libc::proc_listpgrppids(
                process_group_id,
                process_ids.as_mut_ptr().cast(),
                buffer_size,
            )
        };
        if count == 0
            && let Some(error) = current_errno()
        {
            if error.raw_os_error() == Some(libc::ESRCH) {
                return Ok(Vec::new());
            }
            return Err(error);
        }
        if count < 0 {
            return Err(current_errno().unwrap_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "process-group enumeration returned a negative count",
                )
            }));
        }
        let count = count as usize;
        if count < process_ids.len() {
            process_ids.truncate(count);
            return Ok(process_ids);
        }
        let capacity = process_ids.len().checked_mul(2).ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "process group is too large")
        })?;
        process_ids.resize(capacity, 0);
    }
}

fn process_is_live_group_member(
    process_id: libc::pid_t,
    process_group_id: libc::pid_t,
) -> io::Result<bool> {
    let mut information = std::mem::MaybeUninit::<libc::proc_bsdshortinfo>::zeroed();
    let information_size = libc::c_int::try_from(std::mem::size_of_val(&information))
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "process info is too large"))?;
    clear_errno();
    // SAFETY: `information` is writable for `information_size` bytes and the
    // positive PID came from the kernel's process-group snapshot.
    let result = unsafe {
        libc::proc_pidinfo(
            process_id,
            libc::PROC_PIDT_SHORTBSDINFO,
            0,
            information.as_mut_ptr().cast(),
            information_size,
        )
    };
    if result == 0 {
        return match current_errno() {
            Some(error) if error.raw_os_error() != Some(libc::ESRCH) => Err(error),
            Some(_) => Ok(false),
            None => Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "process status returned no data without an error",
            )),
        };
    }
    if result != information_size {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "process status had an unexpected size",
        ));
    }
    // SAFETY: `proc_pidinfo` reported that it initialized the complete struct.
    let information = unsafe { information.assume_init() };
    Ok(information.pbsi_pgid == process_group_id as u32 && information.pbsi_status != libc::SZOMB)
}

fn clear_errno() {
    // SAFETY: `__error` returns the calling thread's valid errno pointer.
    unsafe { *libc::__error() = 0 };
}

fn current_errno() -> Option<io::Error> {
    // SAFETY: `__error` returns the calling thread's valid errno pointer.
    let error = unsafe { *libc::__error() };
    (error != 0).then(|| io::Error::from_raw_os_error(error))
}

fn process_group_of(process_id: libc::pid_t) -> io::Result<Option<libc::pid_t>> {
    // SAFETY: callers pass a positive PID returned by the kernel or the live
    // sandbox child PID.
    let process_group_id = unsafe { libc::getpgid(process_id) };
    if process_group_id < 0 {
        let error = io::Error::last_os_error();
        if error.raw_os_error() == Some(libc::ESRCH) {
            return Ok(None);
        }
        return Err(error);
    }
    Ok(Some(process_group_id))
}

fn kill_process(process_id: libc::pid_t) -> io::Result<()> {
    // SAFETY: callers validate that the PID is positive and still belongs to
    // the expected process group.
    if unsafe { libc::kill(process_id, libc::SIGKILL) } < 0 {
        let error = io::Error::last_os_error();
        if error.raw_os_error() == Some(libc::ESRCH) {
            return Ok(());
        }
        return Err(error);
    }
    Ok(())
}

pub(super) fn sandboxed_command() -> Result<(Command, TemporaryDirectory), String> {
    let temporary_directory = TemporaryDirectory::new()?;
    let mut launcher = Command::new(SANDBOX_EXEC);
    // Do not rely on SIP to keep host interposers out of the Apple sandbox
    // intermediary and the sandbox target.
    launcher
        .env_remove("DYLD_INSERT_LIBRARIES")
        .arg("-p")
        .arg(POLICY)
        .arg(parameter_definition(
            "TEMP_DIRECTORY",
            temporary_directory.path(),
        ))
        .arg("--");

    Ok((launcher, temporary_directory))
}

pub(crate) struct TemporaryDirectory {
    path: PathBuf,
    remove_on_drop: bool,
}

impl TemporaryDirectory {
    fn new() -> Result<Self, String> {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|error| format!("failed to read the system clock: {error}"))?
            .as_nanos();
        let path =
            std::env::temp_dir().join(format!("mcp-console-tmp-{}-{unique}", std::process::id()));

        DirBuilder::new()
            .mode(0o700)
            .create(&path)
            .map_err(|error| {
                format!(
                    "failed to create temporary directory `{}`: {error}",
                    path.display()
                )
            })?;

        let mut temporary_directory = Self {
            path,
            remove_on_drop: true,
        };
        temporary_directory.path = temporary_directory.path.canonicalize().map_err(|error| {
            format!(
                "failed to resolve temporary directory `{}`: {error}",
                temporary_directory.path.display()
            )
        })?;
        Ok(temporary_directory)
    }

    pub(super) fn path(&self) -> &Path {
        &self.path
    }

    pub(crate) fn adopt(path: PathBuf, owner_pid: libc::pid_t) -> Result<Self, String> {
        if owner_pid <= 0 {
            return Err("sandbox temporary directory has invalid ownership".to_string());
        }

        let path = path.canonicalize().map_err(|error| {
            format!(
                "failed to resolve sandbox temporary directory {}: {error}",
                path.display()
            )
        })?;
        let expected_prefix = format!("mcp-console-tmp-{owner_pid}-");
        let valid_name = path
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.starts_with(&expected_prefix));
        let expected_parent = std::env::temp_dir().canonicalize().map_err(|error| {
            format!("failed to resolve the system temporary directory: {error}")
        })?;
        if !valid_name || path.parent() != Some(expected_parent.as_path()) {
            return Err("sandbox temporary directory has invalid ownership".to_string());
        }
        if !path.is_dir() {
            return Err(format!(
                "sandbox temporary directory {} is not a directory",
                path.display()
            ));
        }
        Ok(Self {
            path,
            // The adopting manager must prove cleanup before removing the
            // directory. Preserve it if the manager unwinds unexpectedly.
            remove_on_drop: false,
        })
    }

    /// Leaves the directory in place because cleanup could not prove that it is unused.
    pub(crate) fn preserve(mut self) {
        self.remove_on_drop = false;
    }

    /// Arms best-effort removal after cleanup proved that the directory is unused.
    pub(crate) fn remove(mut self) {
        self.remove_on_drop = true;
    }

    /// Transfers cleanup ownership to another guard for the same directory.
    pub(crate) fn relinquish(mut self) {
        self.remove_on_drop = false;
    }
}

impl Drop for TemporaryDirectory {
    fn drop(&mut self) {
        if self.remove_on_drop {
            // Cleanup must not replace the child status if it changed directory modes.
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}

// `sandbox-exec -DNAME=VALUE` supplies values for `(param "NAME")` in the SBPL.
fn parameter_definition(name: &str, path: &Path) -> OsString {
    let mut argument = OsString::from("-D");
    argument.push(name);
    argument.push("=");
    argument.push(path);
    argument
}

pub(super) fn exit_code(status: ExitStatus) -> ExitCode {
    let code = status
        .code()
        .unwrap_or_else(|| 128 + status.signal().unwrap_or(1));
    ExitCode::from(code as u8)
}
