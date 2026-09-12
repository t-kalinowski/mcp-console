use std::os::fd::RawFd;
#[cfg(target_os = "linux")]
use std::os::fd::{AsRawFd as _, FromRawFd as _, OwnedFd};
use std::os::unix::process::CommandExt as _;
use std::process::Command;

#[cfg(target_os = "macos")]
pub(crate) fn close_unlisted_from_multithreaded_parent(
    command: &mut Command,
) -> Result<(), String> {
    // Enumerate after fork: other parent threads can open descriptors before
    // spawn. Allocate the table beforehand so the child uses only syscalls;
    // scanning every possible slot is expensive with macOS's large fd limit.
    let descriptor_limit = descriptor_limit()?;
    let mut descriptors = vec![
        libc::proc_fdinfo {
            proc_fd: 0,
            proc_fdtype: 0
        };
        descriptor_limit as usize
    ];
    unsafe {
        command.pre_exec(move || {
            let bytes = loop {
                let bytes = libc::proc_pidinfo(
                    libc::getpid(),
                    libc::PROC_PIDLISTFDS,
                    0,
                    descriptors.as_mut_ptr().cast(),
                    std::mem::size_of_val(descriptors.as_slice()) as libc::c_int,
                );
                if bytes > 0 {
                    break bytes as usize;
                }
                let error = std::io::Error::last_os_error();
                if error.kind() != std::io::ErrorKind::Interrupted {
                    return Err(error);
                }
            };
            if !bytes.is_multiple_of(std::mem::size_of::<libc::proc_fdinfo>()) {
                return Err(std::io::ErrorKind::InvalidData.into());
            }
            for descriptor in &descriptors[..bytes / std::mem::size_of::<libc::proc_fdinfo>()] {
                if descriptor.proc_fd > libc::STDERR_FILENO {
                    // Preserve Rust's close-on-exec spawn-error pipe until exec.
                    set_close_on_exec(descriptor.proc_fd)?;
                }
            }
            Ok(())
        });
    }
    Ok(())
}

#[cfg(target_os = "macos")]
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

#[cfg(target_os = "macos")]
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
    let flags = loop {
        let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
        if flags >= 0 {
            break flags;
        }
        let error = std::io::Error::last_os_error();
        match error.raw_os_error() {
            Some(libc::EINTR) => continue,
            Some(libc::EBADF) => return Ok(()),
            _ => return Err(error),
        }
    };
    let updated_flags = flags | libc::FD_CLOEXEC;
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
            Some(libc::EBADF) => return Ok(()),
            _ => return Err(error),
        }
    }
}

#[cfg(target_os = "linux")]
pub(crate) fn close_unlisted_from_multithreaded_parent(
    command: &mut Command,
) -> Result<(), String> {
    unsafe {
        command.pre_exec(|| {
            // Retain Rust's exec-error pipe until exec. Older kernels either
            // lack close_range (ENOSYS) or its CLOEXEC flag (EINVAL). Seccomp
            // can deny it (EPERM) while still allowing procfs and fcntl.
            if libc::syscall(
                libc::SYS_close_range,
                3u32,
                u32::MAX,
                libc::CLOSE_RANGE_CLOEXEC,
            ) < 0
            {
                let error = std::io::Error::last_os_error();
                return match error.raw_os_error() {
                    Some(libc::ENOSYS | libc::EINVAL | libc::EPERM) => cloexec_proc_descriptors(),
                    _ => Err(error),
                };
            }
            Ok(())
        });
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn cloexec_proc_descriptors() -> std::io::Result<()> {
    // Enumerate in the child: another parent thread can open descriptors after
    // a parent-side snapshot, and existing descriptors can exceed a lowered
    // RLIMIT_NOFILE. Use only stack storage and syscalls after fork, avoiding
    // libc directory streams and their allocator locks.
    let directory = loop {
        let descriptor = unsafe {
            libc::open(
                c"/proc/self/fd".as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
            )
        };
        if descriptor >= 0 {
            break unsafe { OwnedFd::from_raw_fd(descriptor) };
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(error);
        }
    };
    let mut buffer = [0u8; 8192];
    loop {
        let count = unsafe {
            libc::syscall(
                libc::SYS_getdents64,
                directory.as_raw_fd(),
                buffer.as_mut_ptr(),
                buffer.len(),
            )
        };
        if count < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error);
        }
        if count == 0 {
            return Ok(());
        }
        let mut offset = 0;
        while offset < count as usize {
            // Linux getdents64 records have a 19-byte header, followed by the
            // NUL-terminated name. The kernel supplies the record boundaries.
            let length = u16::from_ne_bytes([buffer[offset + 16], buffer[offset + 17]]) as usize;
            let name = &buffer[offset + 19..offset + length];
            if name[0].is_ascii_digit() {
                let descriptor = name
                    .iter()
                    .take_while(|byte| **byte != 0)
                    .fold(0, |number, byte| number * 10 + i32::from(byte - b'0'));
                if descriptor > libc::STDERR_FILENO {
                    set_close_on_exec(descriptor)?;
                }
            }
            offset += length;
        }
    }
}
