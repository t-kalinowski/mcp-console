//! Bounded, cancellable descriptor transfer for the remote launch operation.

use std::fs::File;
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, RawFd};
use std::time::Instant;

pub(super) fn duplicate(descriptor: RawFd) -> Result<File, String> {
    let fd = unsafe { libc::fcntl(descriptor, libc::F_DUPFD_CLOEXEC, 3) };
    if fd < 0 {
        return Err(io::Error::last_os_error().to_string());
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

pub(super) fn poll(
    descriptors: &[(RawFd, libc::c_short)],
    deadline: Option<Instant>,
) -> Result<Vec<libc::c_short>, String> {
    let mut descriptors = descriptors
        .iter()
        .map(|&(fd, events)| libc::pollfd {
            fd,
            events,
            revents: 0,
        })
        .collect::<Vec<_>>();
    loop {
        let timeout = deadline.map_or(-1, |deadline| {
            let remaining = deadline.saturating_duration_since(Instant::now());
            (remaining.as_millis()
                + u128::from(!remaining.subsec_nanos().is_multiple_of(1_000_000)))
            .min(i32::MAX as u128) as i32
        });
        let result =
            unsafe { libc::poll(descriptors.as_mut_ptr(), descriptors.len() as _, timeout) };
        if result > 0 {
            return Ok(descriptors.iter().map(|event| event.revents).collect());
        }
        if result == 0 {
            return Err("SSH connection/bootstrap deadline exceeded".into());
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error.to_string());
        }
    }
}

pub(super) struct Io<T> {
    inner: T,
    cancelled: Option<io::PipeReader>,
    deadline: Option<Instant>,
}

impl<T: AsRawFd> Io<T> {
    pub(super) fn into_inner(self) -> T {
        self.inner
    }
    pub(super) fn new(
        inner: T,
        cancelled: Option<io::PipeReader>,
        deadline: Option<Instant>,
    ) -> Result<Self, String> {
        let flags = unsafe { libc::fcntl(inner.as_raw_fd(), libc::F_GETFL) };
        if flags < 0
            || unsafe { libc::fcntl(inner.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) }
                < 0
        {
            return Err(io::Error::last_os_error().to_string());
        }
        Ok(Self {
            inner,
            cancelled,
            deadline,
        })
    }

    fn wait(&self, events: libc::c_short) -> io::Result<()> {
        let ready = poll(
            &[
                (self.inner.as_raw_fd(), events),
                (
                    self.cancelled.as_ref().map_or(-1, AsRawFd::as_raw_fd),
                    libc::POLLIN,
                ),
            ],
            self.deadline,
        )
        .map_err(io::Error::other)?;
        if ready[1] != 0 {
            return Err(io::Error::other("SSH transfer cancelled"));
        }
        Ok(())
    }
}

impl<T: AsRawFd + Read> Read for Io<T> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        loop {
            self.wait(libc::POLLIN)?;
            match self.inner.read(buffer) {
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) => {}
                result => return result,
            }
        }
    }
}

impl<T: AsRawFd + Write> Write for Io<T> {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        loop {
            match self.inner.write(buffer) {
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) =>
                {
                    // Bootstrap has no input owner yet. After one supplies a
                    // cancellation pipe, let that owner arbitrate input closure
                    // independently of this backpressured output stream.
                    let ready = poll(
                        &[
                            (self.inner.as_raw_fd(), libc::POLLOUT),
                            (
                                self.cancelled.as_ref().map_or(-1, AsRawFd::as_raw_fd),
                                libc::POLLIN,
                            ),
                            (if self.cancelled.is_none() { 0 } else { -1 }, 0),
                        ],
                        self.deadline,
                    )
                    .map_err(io::Error::other)?;
                    if ready[1] != 0 || ready[2] != 0 {
                        return Err(io::Error::other(
                            "SSH connection closed or transfer cancelled",
                        ));
                    }
                }
                result => return result,
            }
        }
    }
    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}
