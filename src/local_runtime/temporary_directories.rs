//! Directory-relative permission restoration after direct-worker retirement.

use std::ffi::{CStr, CString};
use std::io;
use std::os::fd::{AsRawFd, FromRawFd, IntoRawFd, OwnedFd, RawFd};
use std::os::unix::ffi::OsStrExt;
use std::path::Path;
use std::ptr::NonNull;

pub(super) fn unlock(path: &Path) -> io::Result<()> {
    let parent = CString::new(
        path.parent()
            .expect("temporary directory parent")
            .as_os_str()
            .as_bytes(),
    )?;
    // A TMPDIR parent needs search and write access, but need not be readable.
    #[cfg(target_os = "linux")]
    let access = libc::O_PATH;
    #[cfg(not(target_os = "linux"))]
    let access = libc::O_SEARCH;
    let descriptor = unsafe {
        libc::open(
            parent.as_ptr(),
            access | libc::O_DIRECTORY | libc::O_CLOEXEC,
        )
    };
    if descriptor < 0 {
        return Err(io::Error::last_os_error());
    }
    let parent = unsafe { OwnedFd::from_raw_fd(descriptor) };
    let name = CString::new(
        path.file_name()
            .expect("temporary directory name")
            .as_bytes(),
    )?;
    unlock_at(parent.as_raw_fd(), &name)
}

fn unlock_at(parent: RawFd, name: &CStr) -> io::Result<()> {
    let metadata = metadata(parent, name)?;
    if metadata.st_mode & libc::S_IFMT != libc::S_IFDIR {
        return Ok(());
    }
    if metadata.st_mode & libc::S_IRUSR == 0 {
        // An unreadable directory cannot be opened for traversal. Restoring
        // its access requires no-follow chmod support; an unavailable native
        // operation (including older Linux without procfs) remains a failure.
        let changed = unsafe {
            libc::fchmodat(
                parent,
                name.as_ptr(),
                metadata.st_mode | 0o700,
                libc::AT_SYMLINK_NOFOLLOW,
            )
        };
        if changed < 0 {
            let error = io::Error::last_os_error();
            // Linux cannot chmod a symlink. A raced replacement must remain a
            // link, and must never authorize chmod of its external target.
            if error.raw_os_error() == Some(libc::ENOTSUP)
                && self::metadata(parent, name)?.st_mode & libc::S_IFMT == libc::S_IFLNK
            {
                return Ok(());
            }
            return Err(error);
        }
    }
    let mut entries = match Entries::open(parent, name) {
        Ok(entries) => entries,
        // A directory replaced by a file or symlink needs no traversal.
        Err(error) if matches!(error.raw_os_error(), Some(libc::ENOTDIR | libc::ELOOP)) => {
            return Ok(());
        }
        Err(error) => return Err(error),
    };
    let descriptor = unsafe { libc::dirfd(entries.0.as_ptr()) };
    let mut current = std::mem::MaybeUninit::uninit();
    if unsafe { libc::fstat(descriptor, current.as_mut_ptr()) } < 0 {
        return Err(io::Error::last_os_error());
    }
    let current = unsafe { current.assume_init() };
    // Readable directories use their pinned object, without fchmodat2 or procfs.
    if unsafe { libc::fchmod(descriptor, current.st_mode | 0o700) } < 0 {
        return Err(io::Error::last_os_error());
    }
    for entry in &mut entries {
        let name = entry?;
        if name.as_bytes() == b"." || name.as_bytes() == b".." {
            continue;
        }
        match unlock_at(descriptor, &name) {
            Ok(()) => {}
            // A vanished child does not settle retirement of the owned root.
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

fn metadata(parent: RawFd, name: &CStr) -> io::Result<libc::stat> {
    let mut metadata = std::mem::MaybeUninit::uninit();
    let result = unsafe {
        libc::fstatat(
            parent,
            name.as_ptr(),
            metadata.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    };
    if result < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(unsafe { metadata.assume_init() })
    }
}

struct Entries(NonNull<libc::DIR>);

impl Entries {
    fn open(parent: RawFd, name: &CStr) -> io::Result<Self> {
        let descriptor = unsafe {
            libc::openat(
                parent,
                name.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if descriptor < 0 {
            return Err(io::Error::last_os_error());
        }
        let descriptor = unsafe { OwnedFd::from_raw_fd(descriptor) };
        let directory = NonNull::new(unsafe { libc::fdopendir(descriptor.as_raw_fd()) })
            .ok_or_else(io::Error::last_os_error)?;
        // fdopendir owns the descriptor on success; closedir releases it.
        let _ = descriptor.into_raw_fd();
        Ok(Self(directory))
    }
}

impl Iterator for Entries {
    type Item = io::Result<CString>;

    fn next(&mut self) -> Option<Self::Item> {
        errno::set_errno(errno::Errno(0));
        let entry = unsafe { libc::readdir(self.0.as_ptr()) };
        if entry.is_null() {
            let error = errno::errno().0;
            return (error != 0).then(|| Err(io::Error::from_raw_os_error(error)));
        }
        Some(Ok(
            unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }.to_owned()
        ))
    }
}

impl Drop for Entries {
    fn drop(&mut self) {
        unsafe { libc::closedir(self.0.as_ptr()) };
    }
}
