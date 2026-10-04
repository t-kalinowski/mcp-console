//! Unix resolvers own a process group and retain the leader until reaping.
//! Group signalling retires trusted host preparation; it is not a sandbox.

use std::io;
use std::os::unix::process::CommandExt as _;
use std::path::Path;
pub(crate) use std::process::Child;
use std::process::{Command, ExitStatus};

use super::{ResolverInterrupt, settle_observation};
use crate::process_exit::ChildExitWaiter;

pub(crate) fn resolver_command(program: &Path) -> Command {
    let mut command = Command::new(program);
    command.process_group(0);
    // SAFETY: the closure calls only libc signal functions after fork and
    // before exec. Resolver programs must not inherit an ignored or blocked
    // SIGINT from the MCP host.
    unsafe {
        command.pre_exec(|| {
            if libc::signal(libc::SIGINT, libc::SIG_DFL) == libc::SIG_ERR {
                return Err(io::Error::last_os_error());
            }
            let mut signals = std::mem::zeroed();
            if libc::sigemptyset(&mut signals) != 0
                || libc::sigaddset(&mut signals, libc::SIGINT) != 0
                || libc::sigprocmask(libc::SIG_UNBLOCK, &signals, std::ptr::null_mut()) != 0
            {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        });
    }
    command
}

pub(crate) fn spawn_resolver(command: &mut Command) -> io::Result<Child> {
    command.spawn()
}

pub(super) fn interrupt_resolver(child: &mut Child) -> io::Result<ResolverInterrupt> {
    let pid = child.id();
    // SAFETY: `process_group(0)` made the resolver PID its process-group ID.
    if unsafe { libc::killpg(pid as libc::pid_t, libc::SIGINT) } == 0 {
        return Ok(ResolverInterrupt::Signaled);
    }
    let error = io::Error::last_os_error();
    if matches!(error.raw_os_error(), Some(libc::EPERM) | Some(libc::ESRCH)) {
        // Keep an exited leader unreaped so its watcher remains authoritative
        // and this resolver PID cannot be reused before normal cleanup.
        if crate::process_exit::direct_child_has_exited(pid)? {
            return Ok(ResolverInterrupt::AlreadyExited);
        }
    }
    Err(error)
}

pub(super) fn stop_resolver(
    child: &mut Child,
    program: &Path,
    kind: &str,
    exit: Option<&mut ChildExitWaiter>,
) -> Result<ExitStatus, String> {
    // SAFETY: `process_group(0)` made the resolver PID its process-group ID.
    let result = unsafe { libc::killpg(child.id() as libc::pid_t, libc::SIGKILL) };
    if result < 0 {
        let kill_error = io::Error::last_os_error();
        match crate::process_exit::direct_child_has_exited(child.id()) {
            // macOS reports EPERM when only the unreaped group leader remains.
            // ESRCH likewise means there is no remaining group to stop.
            Ok(true)
                if matches!(
                    kill_error.raw_os_error(),
                    Some(libc::EPERM) | Some(libc::ESRCH)
                ) => {}
            Ok(_) => {
                return Err(format!(
                    "failed to stop {kind} resolver `{}`: {kill_error}",
                    program.display()
                ));
            }
            Err(wait_error) => {
                return Err(format!(
                    "failed to stop {kind} resolver `{}`: {kill_error}; additionally failed to read its status: {wait_error}",
                    program.display()
                ));
            }
        }
    }
    settle_observation(exit);
    child.wait().map_err(|error| {
        format!(
            "failed to reap {kind} resolver `{}`: {error}",
            program.display()
        )
    })
}
