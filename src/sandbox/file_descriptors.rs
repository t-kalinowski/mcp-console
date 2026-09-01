use std::os::fd::RawFd;
use std::os::unix::process::CommandExt as _;
use std::process::Command;

pub(super) fn close_unlisted_except(
    command: &mut Command,
    inherited_descriptor: RawFd,
) -> Result<(), String> {
    // The standalone path reaches this point before starting any threads, so
    // this snapshot contains every inherited descriptor that can reach the
    // child. Change flags only after fork to leave the launcher unchanged.
    // Rust creates its later exec-error pipe with close-on-exec already set.
    let mut descriptors = open_descriptors()?;
    if inherited_descriptor <= libc::STDERR_FILENO || !descriptors.contains(&inherited_descriptor) {
        return Err("sandbox inherited descriptor is invalid".to_string());
    }
    descriptors.retain(|descriptor| *descriptor > libc::STDERR_FILENO);
    unsafe {
        command.pre_exec(move || {
            for descriptor in &descriptors {
                if *descriptor == inherited_descriptor {
                    clear_close_on_exec(*descriptor)?;
                } else {
                    set_close_on_exec(*descriptor)?;
                }
            }
            Ok(())
        });
    }
    Ok(())
}

pub(super) fn close_unlisted_from_multithreaded_parent(
    command: &mut Command,
) -> Result<(), String> {
    // A server thread can open a descriptor after any parent-side snapshot.
    // Scan every possible child slot after fork instead. Descriptors created by
    // Rust for spawn failure reporting already carry close-on-exec and remain
    // usable until a successful exec closes them.
    let descriptor_limit = descriptor_limit()?;
    unsafe {
        command.pre_exec(move || {
            for descriptor in (libc::STDERR_FILENO + 1)..descriptor_limit {
                set_close_on_exec(descriptor)?;
            }
            Ok(())
        });
    }
    Ok(())
}

fn descriptor_limit() -> Result<RawFd, String> {
    let table_size = unsafe { libc::getdtablesize() };
    if table_size <= 0 {
        return Err(format!(
            "failed to read the launcher file-descriptor limit: {}",
            std::io::Error::last_os_error()
        ));
    }
    Ok(open_descriptors()?
        .into_iter()
        .max()
        .map_or(table_size, |descriptor| {
            table_size.max(descriptor.saturating_add(1))
        }))
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

fn set_close_on_exec(descriptor: RawFd) -> std::io::Result<()> {
    update_close_on_exec(descriptor, true, true)
}

fn clear_close_on_exec(descriptor: RawFd) -> std::io::Result<()> {
    update_close_on_exec(descriptor, false, false)
}

fn update_close_on_exec(
    descriptor: RawFd,
    close_on_exec: bool,
    ignore_missing: bool,
) -> std::io::Result<()> {
    let flags = loop {
        let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
        if flags >= 0 {
            break flags;
        }
        let error = std::io::Error::last_os_error();
        match error.raw_os_error() {
            Some(libc::EINTR) => continue,
            Some(libc::EBADF) if ignore_missing => return Ok(()),
            _ => return Err(error),
        }
    };
    let updated_flags = if close_on_exec {
        flags | libc::FD_CLOEXEC
    } else {
        flags & !libc::FD_CLOEXEC
    };
    if flags == updated_flags {
        return Ok(());
    }

    loop {
        if unsafe { libc::fcntl(descriptor, libc::F_SETFD, updated_flags) } == 0 {
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        match error.raw_os_error() {
            Some(libc::EINTR) => continue,
            Some(libc::EBADF) if ignore_missing => return Ok(()),
            _ => return Err(error),
        }
    }
}
