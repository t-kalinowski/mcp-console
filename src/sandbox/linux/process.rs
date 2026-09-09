//! Linux lifetime primitives. Every process belongs to a subreaper, and every
//! blocking wait has a descriptor wakeup; no fork-event polling is needed.

use std::fs;
use std::io;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::time::{Duration, Instant};

pub(super) fn pidfd(pid: libc::pid_t) -> io::Result<OwnedFd> {
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(unsafe { OwnedFd::from_raw_fd(fd as RawFd) })
}

pub(super) fn signal(fd: &OwnedFd, number: libc::c_int) -> io::Result<()> {
    if unsafe {
        libc::syscall(
            libc::SYS_pidfd_send_signal,
            fd.as_raw_fd(),
            number,
            std::ptr::null::<libc::siginfo_t>(),
            0,
        )
    } < 0
    {
        let error = io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::ESRCH) {
            return Err(error);
        }
    }
    Ok(())
}

pub(super) fn subreaper() -> io::Result<()> {
    if unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

pub(super) fn children(pid: libc::pid_t) -> io::Result<Vec<libc::pid_t>> {
    let mut children = Vec::new();
    for task in fs::read_dir(format!("/proc/{pid}/task"))? {
        let path = task?.path().join("children");
        match fs::read_to_string(path) {
            Ok(text) => children.extend(
                text.split_whitespace()
                    .map(|pid| pid.parse::<libc::pid_t>().expect("kernel child PID")),
            ),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    children.sort_unstable();
    children.dedup();
    Ok(children)
}

pub(super) fn poll(descriptors: &mut [libc::pollfd], timeout: Option<Duration>) -> io::Result<()> {
    let timeout = timeout.map(|duration| libc::timespec {
        tv_sec: duration.as_secs() as libc::time_t,
        tv_nsec: duration.subsec_nanos() as libc::c_long,
    });
    loop {
        let result = unsafe {
            libc::ppoll(
                descriptors.as_mut_ptr(),
                descriptors.len() as libc::nfds_t,
                timeout.as_ref().map_or(std::ptr::null(), |t| t),
                std::ptr::null(),
            )
        };
        if result >= 0 {
            return Ok(());
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

pub(super) fn watch(fd: RawFd, events: libc::c_short) -> libc::pollfd {
    libc::pollfd {
        fd,
        events,
        revents: 0,
    }
}

/// Kill direct children, then newly adopted orphans. Reap only after signaling
/// each batch so waitable identities stay pinned throughout its termination.
pub(super) fn retire() -> io::Result<()> {
    let deadline = Instant::now() + crate::sandbox::MANAGER_CLEANUP_TIMEOUT;
    loop {
        let children = children(unsafe { libc::getpid() })?;
        if children.is_empty() {
            return Ok(());
        }
        let mut waits = Vec::new();
        for pid in children {
            let fd = pidfd(pid)?;
            signal(&fd, libc::SIGKILL)?;
            waits.push((pid, fd));
        }
        for (pid, fd) in waits {
            let remaining = deadline
                .checked_duration_since(Instant::now())
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::TimedOut, "sandbox descendants did not exit")
                })?;
            let mut descriptors = [watch(fd.as_raw_fd(), libc::POLLIN)];
            poll(&mut descriptors, Some(remaining))?;
            if descriptors[0].revents == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "sandbox descendant did not exit",
                ));
            }
            let mut status = 0;
            if unsafe { libc::waitpid(pid, &mut status, 0) } < 0 {
                return Err(io::Error::last_os_error());
            }
        }
    }
}

/// Native namespace init forwards signals to its command. Address it through
/// a pidfd, leaving the waiting runner and host manager alive to collect status.
/// Signals before namespace creation have no target, as on macOS startup.
pub(super) fn forward(root: libc::pid_t, number: libc::c_int) -> io::Result<()> {
    let mut pending = children(root)?;
    while let Some(pid) = pending.pop() {
        let fd = match pidfd(pid) {
            Ok(fd) => fd,
            Err(error) if error.raw_os_error() == Some(libc::ESRCH) => continue,
            Err(error) => return Err(error),
        };
        match fs::read_to_string(format!("/proc/{pid}/status")) {
            Ok(status) => {
                if status
                    .lines()
                    .find_map(|line| line.strip_prefix("NSpid:"))
                    .is_some_and(|pids| {
                        pids.split_whitespace().count() > 1
                            && pids.split_whitespace().last() == Some("1")
                    })
                {
                    signal(&fd, number)?;
                    return Ok(());
                }
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        }
        match children(pid) {
            Ok(children) => pending.extend(children),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    Ok(())
}
