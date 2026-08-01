use std::os::fd::RawFd;

pub(super) fn close_unlisted_on_exec(allowed: &[RawFd]) -> Result<(), String> {
    // The sandbox subcommand is a dedicated, current-thread launcher. No other
    // thread can open or reuse a descriptor between this snapshot and spawn().
    for descriptor in open_descriptors()? {
        let flags = descriptor_flags(descriptor)?;
        let desired = if allowed.contains(&descriptor) {
            flags & !libc::FD_CLOEXEC
        } else {
            flags | libc::FD_CLOEXEC
        };
        if desired != flags {
            set_descriptor_flags(descriptor, desired)?;
        }
    }
    Ok(())
}

fn open_descriptors() -> Result<Vec<RawFd>, String> {
    let mut capacity = 16;
    loop {
        let mut descriptors: Vec<libc::proc_fdinfo> = Vec::with_capacity(capacity);
        descriptors.resize_with(capacity, || unsafe { std::mem::zeroed() });

        unsafe { *libc::__error() = 0 };
        let size = unsafe {
            libc::proc_pidinfo(
                libc::getpid(),
                libc::PROC_PIDLISTFDS,
                0,
                descriptors.as_mut_ptr().cast(),
                std::mem::size_of_val(descriptors.as_slice()) as libc::c_int,
            )
        };
        if size == 0 {
            let error_code = unsafe { *libc::__error() };
            if error_code == 0 {
                return Ok(Vec::new());
            }
            if error_code == libc::EINTR {
                continue;
            }
            return Err(format!(
                "failed to list launcher file descriptors: {}",
                std::io::Error::from_raw_os_error(error_code)
            ));
        }
        if size < 0 || !(size as usize).is_multiple_of(std::mem::size_of::<libc::proc_fdinfo>()) {
            return Err(format!(
                "failed to list launcher file descriptors: proc_pidinfo returned {size} bytes"
            ));
        }

        let count = size as usize / std::mem::size_of::<libc::proc_fdinfo>();
        if count < capacity {
            descriptors.truncate(count);
            return Ok(descriptors
                .into_iter()
                .map(|descriptor| descriptor.proc_fd)
                .collect());
        }
        capacity = capacity.saturating_mul(2).max(count + 16);
    }
}

fn descriptor_flags(descriptor: RawFd) -> Result<libc::c_int, String> {
    loop {
        let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
        if flags >= 0 {
            return Ok(flags);
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(format!(
                "failed to inspect launcher file descriptor {descriptor}: {error}"
            ));
        }
    }
}

fn set_descriptor_flags(descriptor: RawFd, flags: libc::c_int) -> Result<(), String> {
    loop {
        if unsafe { libc::fcntl(descriptor, libc::F_SETFD, flags) } == 0 {
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(format!(
                "failed to configure launcher file descriptor {descriptor}: {error}"
            ));
        }
    }
}
