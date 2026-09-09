//! The launcher and its manager are subreapers. The manager owns the runner;
//! launcher loss closes control, while manager loss adopts its children into
//! the launcher. Both paths retire all children before removing private data.

mod process;

use super::{
    platform,
    runner::{self, Setup},
};
use process::{pidfd, poll, watch};
use std::ffi::OsString;
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::net::UnixStream;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{ExitCode, ExitStatus};
use std::time::{Duration, Instant};

const FORWARDED: [libc::c_int; 4] = [libc::SIGHUP, libc::SIGINT, libc::SIGQUIT, libc::SIGTERM];

pub(super) fn run(command: &[OsString], owner: Option<u32>) -> Result<ExitCode, String> {
    if let Some(pid) = owner
        && unsafe { libc::getppid() } as u32 != pid
    {
        return Err(format!(
            "sandbox owner {pid} is not the launcher's current parent"
        ));
    }
    run_inner(command, owner).map_err(|error| format!("Linux sandbox: {error}"))
}

fn run_inner(arguments: &[OsString], owner: Option<u32>) -> io::Result<ExitCode> {
    let owner = owner
        .map(|pid| {
            if unsafe { libc::getppid() } as u32 != pid {
                return Err(io::Error::other(format!(
                    "sandbox owner {pid} is not the launcher's current parent"
                )));
            }
            let fd = pidfd(pid as libc::pid_t)?;
            if unsafe { libc::getppid() } as u32 != pid {
                return Err(io::Error::other("sandbox owner exited during startup"));
            }
            Ok(fd)
        })
        .transpose()?;
    let signals = Signals::new()?;
    process::subreaper()?;
    let (command, mut temporary) = platform::sandboxed_command().map_err(io::Error::other)?;
    let (mut control, manager_control) = UnixStream::pair()?;
    // This CLI entry point has not started a runtime or any threads. Fork the
    // host manager while allocator and process state are still single-threaded.
    let manager_pid = unsafe { libc::fork() };
    if manager_pid < 0 {
        return Err(io::Error::last_os_error());
    }
    if manager_pid == 0 {
        drop(control);
        drop(owner);
        let result = manage(command, &temporary, arguments, manager_control, &signals);
        let cleanup = process::retire();
        let cleaned = cleanup.is_ok();
        let code = match result.and_then(|status| cleanup.map(|()| status)) {
            Ok(status) => status,
            Err(error) => {
                eprintln!("Linux sandbox manager: {error}");
                1
            }
        };
        if cleaned {
            drop(temporary);
        }
        // Finish this forked manager without returning through CLI dispatch.
        unsafe { libc::_exit(code) };
    }
    drop(command);
    drop(manager_control);
    let result = (|| {
        let manager = pidfd(manager_pid)?;
        crate::process_descriptors::detach_stdin().map_err(io::Error::other)?;
        let mut deadline: Option<Instant> = None;
        loop {
            let mut waits = vec![
                watch(manager.as_raw_fd(), libc::POLLIN),
                watch(signals.fd.as_raw_fd(), libc::POLLIN),
            ];
            if deadline.is_none()
                && let Some(owner) = &owner
            {
                waits.push(watch(owner.as_raw_fd(), libc::POLLIN));
            }
            poll(
                &mut waits,
                deadline.map(|deadline| deadline.saturating_duration_since(Instant::now())),
            )?;
            if waits.iter().all(|wait| wait.revents == 0) {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "sandbox manager did not retire",
                ));
            }
            if waits[0].revents != 0 {
                break;
            }
            if waits.get(2).is_some_and(|wait| wait.revents != 0) {
                control.shutdown(std::net::Shutdown::Both)?;
                deadline =
                    Some(Instant::now() + super::MANAGER_CLEANUP_TIMEOUT + Duration::from_secs(1));
            }
            if waits[1].revents != 0 {
                while let Some(signal) = signals.take()? {
                    if signal == libc::SIGCHLD {
                        continue;
                    }
                    let byte = if owner.is_some() && signal == libc::SIGTERM {
                        deadline.get_or_insert(
                            Instant::now()
                                + super::MANAGER_CLEANUP_TIMEOUT
                                + Duration::from_secs(1),
                        );
                        0
                    } else {
                        signal as u8
                    };
                    if let Err(error) = control.write_all(&[byte])
                        && error.kind() != io::ErrorKind::BrokenPipe
                    {
                        return Err(error);
                    }
                }
            }
        }
        let mut status = 0;
        if unsafe { libc::waitpid(manager_pid, &mut status, 0) } < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(ExitStatus::from_raw(status))
    })();
    drop(control);
    // Manager failure leaves adopted children here. A normal manager has
    // already reaped its complete lifetime before exiting.
    if let Err(error) = process::retire() {
        temporary.relinquish();
        return Err(error);
    }
    result.map(platform::exit_code)
}

fn manage(
    mut command: std::process::Command,
    temporary: &platform::TemporaryDirectory,
    arguments: &[OsString],
    mut control: UnixStream,
    signals: &Signals,
) -> io::Result<i32> {
    process::subreaper()?;
    let (program, arguments) = arguments.split_first().expect("sandbox command");
    let mut setup = Setup::new(
        &mut command,
        temporary,
        program,
        arguments,
        signals.original,
        signals.ignored,
    )
    .map_err(io::Error::other)?;
    let manager_pid = unsafe { libc::getpid() };
    unsafe {
        command.pre_exec(move || {
            if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL, 0, 0, 0) < 0 {
                return Err(io::Error::last_os_error());
            }
            if libc::getppid() != manager_pid {
                libc::_exit(1);
            }
            Ok(())
        });
    }
    let mut root = command.spawn()?;
    drop(command);
    crate::process_descriptors::detach_stdin().map_err(io::Error::other)?;
    let root_fd = pidfd(root.id() as libc::pid_t)?;
    control.set_nonblocking(true)?;
    loop {
        let mut waits = vec![
            watch(root_fd.as_raw_fd(), libc::POLLIN),
            watch(control.as_raw_fd(), libc::POLLIN),
        ];
        if setup.pending() {
            waits.push(watch(setup.descriptor(), libc::POLLOUT));
        }
        poll(&mut waits, None)?;
        if waits[0].revents != 0 {
            let status = root.wait()?;
            return Ok(status
                .code()
                .unwrap_or_else(|| 128 + status.signal().unwrap_or(1)));
        }
        if waits[1].revents != 0 {
            let mut byte = [0];
            match control.read(&mut byte) {
                Ok(0) => return Ok(0),
                Ok(_) if byte[0] == 0 => return Ok(0),
                Ok(_) => process::forward(root.id() as libc::pid_t, byte[0] as libc::c_int)?,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
                Err(error) => return Err(error),
            }
        }
        if waits.get(2).is_some_and(|wait| wait.revents != 0) {
            setup.write_once()?;
        }
    }
}

struct Signals {
    fd: OwnedFd,
    original: libc::sigset_t,
    ignored: u64,
}

impl Signals {
    fn new() -> io::Result<Self> {
        let ignored = runner::ignored_signals()?;
        let mut set = unsafe { std::mem::zeroed() };
        let mut original = unsafe { std::mem::zeroed() };
        unsafe {
            libc::sigemptyset(&mut set);
            for signal in FORWARDED.into_iter().chain([libc::SIGCHLD]) {
                libc::sigaddset(&mut set, signal);
            }
            let result = libc::pthread_sigmask(libc::SIG_BLOCK, &set, &mut original);
            if result != 0 {
                return Err(io::Error::from_raw_os_error(result));
            }
            // Ignored SIGCHLD would make children un-waitable; ignored SIGTERM
            // must still be retained as an owned launcher's retirement request.
            libc::signal(libc::SIGCHLD, libc::SIG_DFL);
            libc::signal(libc::SIGTERM, libc::SIG_DFL);
            let fd = libc::signalfd(-1, &set, libc::SFD_CLOEXEC | libc::SFD_NONBLOCK);
            if fd < 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(Self {
                fd: OwnedFd::from_raw_fd(fd),
                original,
                ignored,
            })
        }
    }

    fn take(&self) -> io::Result<Option<libc::c_int>> {
        let mut info: libc::signalfd_siginfo = unsafe { std::mem::zeroed() };
        let size = std::mem::size_of_val(&info);
        let count = unsafe {
            libc::read(
                self.fd.as_raw_fd(),
                (&mut info as *mut libc::signalfd_siginfo).cast(),
                size,
            )
        };
        if count == size as isize {
            return Ok(Some(info.ssi_signo as libc::c_int));
        }
        let error = io::Error::last_os_error();
        if error.kind() == io::ErrorKind::WouldBlock {
            Ok(None)
        } else {
            Err(error)
        }
    }
}
