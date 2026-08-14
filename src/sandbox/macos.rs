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
const PROCESS_GROUP_STOP_POLL_INTERVAL: Duration = Duration::from_millis(1);
const PROCESS_GROUP_STOP_TIMEOUT: Duration = Duration::from_secs(1);

pub(super) fn kill_process_group(process_group_id: u32) -> io::Result<()> {
    let process_group_id = libc::pid_t::try_from(process_group_id)
        .ok()
        .filter(|process_group_id| *process_group_id > 0)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid process group ID"))?;

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

    // Group signalling and the EPERM fallback both operate on PID snapshots.
    // Keep the direct leader unreaped while rescanning so its PID pins the
    // process-group identity until the caller collects it.
    let deadline = Instant::now() + PROCESS_GROUP_STOP_TIMEOUT;
    loop {
        let mut members = process_group_members(process_group_id)?;
        if !members.contains(&process_group_id) {
            members.push(process_group_id);
        }
        // Stop the leader before its descendants so it cannot fork after this
        // snapshot. A later pass must observe the exact group as quiescent.
        members.sort_unstable_by_key(|process_id| *process_id != process_group_id);

        let mut observed_live_member = false;
        let mut first_error = None;
        for process_id in members {
            if process_id <= 0 {
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
    launcher
        .arg("-p")
        .arg(POLICY)
        .arg(parameter_definition(
            "TEMP_DIRECTORY",
            temporary_directory.path(),
        ))
        .arg("--");

    Ok((launcher, temporary_directory))
}

pub(super) struct TemporaryDirectory(PathBuf);

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

        let mut directory = Self(path);
        directory.0 = directory.0.canonicalize().map_err(|error| {
            format!(
                "failed to resolve temporary directory `{}`: {error}",
                directory.0.display()
            )
        })?;
        Ok(directory)
    }

    pub(super) fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TemporaryDirectory {
    fn drop(&mut self) {
        // Cleanup must not replace the child status if it changed directory modes.
        let _ = fs::remove_dir_all(&self.0);
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
