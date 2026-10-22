//! Raw capture handles remain independent of the journal writer and its mutex.
use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use super::{create_private_directory, create_private_file};

/// Validate the opened object, then keep that handle through export. A path
/// check alone cannot authorize a later privileged open of worker-writable capture.
pub(super) fn open_capture_image(directory: &Path, relative: &str) -> Result<File, String> {
    let recording = directory.canonicalize().map_err(|e| e.to_string())?;
    let capture = directory
        .join("capture/sessions")
        .canonicalize()
        .map_err(|e| e.to_string())?;
    let path = directory
        .join(relative)
        .canonicalize()
        .map_err(|e| format!("recorded image {relative} unavailable: {e}"))?;
    if !capture.starts_with(&recording) || !path.starts_with(&capture) {
        return Err("recorded image is outside this recording's capture".into());
    }
    let mut options = File::options();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt as _;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt as _;
        options.custom_flags(windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OPEN_REPARSE_POINT);
    }
    let file = options
        .open(path)
        .map_err(|e| format!("recorded image {relative} unavailable: {e}"))?;
    if !file.metadata().map_err(|e| e.to_string())?.is_file() {
        return Err("recorded image is not a regular file".into());
    }
    if !opened_path(&file)?.starts_with(&capture) {
        return Err("opened image is outside this recording's capture".into());
    }
    Ok(file)
}

fn opened_path(file: &File) -> Result<PathBuf, String> {
    #[cfg(target_os = "linux")]
    {
        use std::os::fd::AsRawFd as _;
        std::fs::read_link(format!("/proc/self/fd/{}", file.as_raw_fd())).map_err(|e| e.to_string())
    }
    #[cfg(target_os = "macos")]
    {
        use std::os::fd::AsRawFd as _;
        use std::os::unix::ffi::OsStrExt as _;
        let mut buffer = [0 as libc::c_char; libc::PATH_MAX as usize];
        // SAFETY: F_GETPATH writes at most PATH_MAX bytes and terminates the
        // path on success. The descriptor remains owned by file.
        if unsafe { libc::fcntl(file.as_raw_fd(), libc::F_GETPATH, buffer.as_mut_ptr()) } < 0 {
            return Err(std::io::Error::last_os_error().to_string());
        }
        Ok(PathBuf::from(std::ffi::OsStr::from_bytes(
            unsafe { std::ffi::CStr::from_ptr(buffer.as_ptr()) }.to_bytes(),
        )))
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStringExt as _;
        use std::os::windows::io::AsRawHandle as _;
        use windows_sys::Win32::Storage::FileSystem::GetFinalPathNameByHandleW;
        let mut buffer = vec![0u16; 32768];
        // SAFETY: the live file handle and writable buffer cover this call;
        // zero flags request the normalized DOS path, as canonicalize does.
        let length = unsafe {
            GetFinalPathNameByHandleW(
                file.as_raw_handle(),
                buffer.as_mut_ptr(),
                buffer.len() as u32,
                0,
            )
        } as usize;
        if length == 0 {
            return Err(std::io::Error::last_os_error().to_string());
        }
        if length >= buffer.len() {
            return Err("recorded image path exceeds the Windows path limit".into());
        }
        Ok(std::ffi::OsString::from_wide(&buffer[..length]).into())
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
    {
        let _ = file;
        Err("session image export is unavailable on this platform".into())
    }
}

#[derive(Default)]
pub(super) struct CaptureStorage {
    files: Mutex<Option<CaptureFiles>>,
    pub(super) public_directory: Arc<Mutex<PathBuf>>,
}

struct CaptureFiles {
    temporary: Option<tempfile::TempDir>,
    directory: PathBuf,
    active: BTreeMap<String, std::sync::Weak<Mutex<CaptureFile>>>,
}

/// Publication relocates live producers while completed files have no writer.
pub(super) struct CaptureFile {
    pub(super) writer: Option<File>,
    pub(super) path: PathBuf,
    complete: bool,
}

impl CaptureStorage {
    fn with_files<T>(
        &self,
        operation: impl FnOnce(&mut CaptureFiles) -> Result<T, String>,
    ) -> Result<T, String> {
        let mut state = self
            .files
            .lock()
            .map_err(|_| "capture storage lock poisoned")?;
        if state.is_none() {
            let temporary = tempfile::tempdir()
                .map_err(|e| format!("failed to create temporary capture: {e}"))?;
            let directory = temporary.path().to_owned();
            create_private_directory(&directory.join("capture/sessions"), true)
                .map_err(|e| e.to_string())?;
            *state = Some(CaptureFiles {
                temporary: Some(temporary),
                directory,
                active: BTreeMap::new(),
            });
        }
        operation(state.as_mut().expect("initialized capture storage"))
    }

    pub(super) fn directory(&self) -> Result<PathBuf, String> {
        self.with_files(|files| Ok(files.directory.clone()))
    }

    pub(super) fn create_file(&self, relative: &str) -> Result<Arc<Mutex<CaptureFile>>, String> {
        self.with_files(|files| {
            let path = files.directory.join(relative);
            create_private_directory(path.parent().expect("capture parent"), true)
                .map_err(|e| e.to_string())?;
            let public_path = self
                .public_directory
                .lock()
                .map_err(|_| "recording path lock poisoned")?
                .join(relative);
            let writer = create_private_file(&path)
                .map_err(|e| format!("failed to create {}: {e}", public_path.display()))?;
            let file = Arc::new(Mutex::new(CaptureFile {
                writer: Some(writer),
                path,
                complete: false,
            }));
            files.active.retain(|_, file| file.strong_count() != 0);
            files
                .active
                .insert(relative.to_owned(), Arc::downgrade(&file));
            Ok(file)
        })
    }

    pub(super) fn relocate(&self, destination: &Path) -> Result<(), String> {
        self.with_files(|files| {
            // No journal or projection write runs while this registry is held.
            for (relative, file) in &files.active {
                if let Some(file) = file.upgrade() {
                    file.lock()
                        .map_err(|_| "capture file lock poisoned")?
                        .relocate(&destination.join(relative))?;
                }
            }
            copy_capture(&files.directory, destination)?;
            files.directory = destination.to_owned();
            Ok(())
        })
    }

    pub(super) fn finish_publication(&self) -> Result<(), String> {
        // Keep scratch ownership if reopening a journal/projection fails. Its
        // read-only files still need explicit cleanup after those handles retire.
        let temporary = self.with_files(|files| Ok(files.temporary.take()))?;
        if let Some(temporary) = temporary {
            discard_temporary_capture(temporary);
        }
        Ok(())
    }

    pub(super) fn finalize(&self) -> Result<(), String> {
        let mut state = self
            .files
            .lock()
            .map_err(|_| "capture storage lock poisoned")?;
        let Some(files) = state.as_mut() else {
            return Ok(());
        };
        {
            let mut errors = Vec::new();
            for file in files.active.values().filter_map(std::sync::Weak::upgrade) {
                if let Err(error) = file
                    .lock()
                    .map_err(|_| "capture file lock poisoned".to_string())
                    .and_then(|mut f| f.finish())
                {
                    errors.push(error);
                }
            }
            files.active.clear();
            if let Err(error) = finalize_files(&files.directory) {
                errors.push(error);
            }
            if errors.is_empty() {
                Ok(())
            } else {
                Err(errors.join("; "))
            }
        }
    }
}

impl Drop for CaptureFiles {
    fn drop(&mut self) {
        if let Some(temporary) = self.temporary.take() {
            discard_temporary_capture(temporary);
        }
    }
}

pub(super) fn discard_temporary_capture(temporary: tempfile::TempDir) {
    // Windows deletion can reject read-only files. This tree is private scratch
    // storage; clearing the flag here never changes the published recording.
    #[cfg(windows)]
    // This changes the Windows file attribute, preserving its existing ACL.
    // The lint's world-writable Unix behavior cannot occur in this function.
    #[allow(clippy::permissions_set_readonly_false)]
    fn clear_readonly(directory: &Path) -> std::io::Result<()> {
        for entry in std::fs::read_dir(directory)? {
            let entry = entry?;
            if entry.file_type()?.is_dir() {
                clear_readonly(&entry.path())?;
            } else {
                let mut permissions = entry.metadata()?.permissions();
                permissions.set_readonly(false);
                std::fs::set_permissions(entry.path(), permissions)?;
            }
        }
        Ok(())
    }
    #[cfg(windows)]
    if let Err(error) = clear_readonly(temporary.path()) {
        let _ = writeln!(
            std::io::stderr().lock(),
            "mcp-console: temporary recording cleanup failed: {error}"
        );
    }
    if let Err(error) = temporary.close() {
        let _ = writeln!(
            std::io::stderr().lock(),
            "mcp-console: temporary recording cleanup failed: {error}"
        );
    }
}
impl CaptureFile {
    pub(super) fn finish(&mut self) -> Result<(), String> {
        if self.complete {
            return Ok(());
        }
        if let Some(mut writer) = self.writer.take() {
            writer.flush().map_err(|e| e.to_string())?;
        }
        make_read_only(&self.path)?;
        self.complete = true;
        Ok(())
    }
    fn relocate(&mut self, destination: &Path) -> Result<(), String> {
        if let Some(writer) = &mut self.writer {
            writer.flush().map_err(|e| e.to_string())?;
        }
        create_private_directory(destination.parent().expect("capture parent"), true)
            .map_err(|e| e.to_string())?;
        std::fs::copy(&self.path, destination).map_err(|e| e.to_string())?;
        self.writer = if self.complete {
            None
        } else {
            Some(
                OpenOptions::new()
                    .append(true)
                    .open(destination)
                    .map_err(|e| e.to_string())?,
            )
        };
        self.path = destination.to_owned();
        Ok(())
    }
}
fn copy_capture(source: &Path, destination: &Path) -> Result<(), String> {
    for entry in std::fs::read_dir(source).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let target = destination.join(entry.file_name());
        if entry.file_type().map_err(|e| e.to_string())?.is_dir() {
            create_private_directory(&target, true).map_err(|e| e.to_string())?;
            copy_capture(&entry.path(), &target)?;
        } else if !target.exists() {
            std::fs::copy(entry.path(), target).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}
fn finalize_files(directory: &Path) -> Result<(), String> {
    let mut errors = Vec::new();
    for entry in std::fs::read_dir(directory).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let kind = entry.file_type().map_err(|e| e.to_string())?;
        let result = if kind.is_dir() {
            finalize_files(&entry.path())
        } else if kind.is_file() {
            make_read_only(&entry.path())
        } else {
            Ok(())
        };
        if let Err(error) = result {
            errors.push(error);
        }
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join("; "))
    }
}
fn make_read_only(path: &Path) -> Result<(), String> {
    let mut permissions = std::fs::metadata(path)
        .map_err(|e| e.to_string())?
        .permissions();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt as _;
        permissions.set_mode(permissions.mode() & !0o222);
    }
    #[cfg(not(unix))]
    permissions.set_readonly(true);
    std::fs::set_permissions(path, permissions)
        .map_err(|e| format!("failed to make {} read-only: {e}", path.display()))
}

impl super::Transcript {
    pub(crate) fn persist_decoded_image(
        &self,
        call_id: Option<u64>,
        bytes: &[u8],
        mime_type: &str,
        encoded_bytes: usize,
    ) -> Option<super::Artifact> {
        let (id, owner, stem) = self.reserve_image(bytes.len(), encoded_bytes, mime_type)?;
        let extension = if mime_type == "image/png" {
            "png"
        } else {
            "bin"
        };
        let path = format!("{stem}.{extension}");
        let retained = (|| {
            let file = self.3.create_file(&path)?;
            let mut file = file.lock().map_err(|_| "image lock poisoned")?;
            file.writer
                .as_mut()
                .expect("image writer")
                .write_all(bytes)
                .map_err(|e| e.to_string())?;
            file.finish()
        })();
        if let Err(error) = retained {
            self.disable(error);
            return None;
        }
        self.queue_event(super::queue::QueuedEvent::ImageRetained {
            owner,
            artifact_id: id,
            call_id,
            path: path.clone(),
            mime_type: mime_type.to_owned(),
            bytes: bytes.len(),
        });
        Some(super::Artifact {
            id,
            path,
            mime_type: mime_type.to_owned(),
            owner: Some(owner),
            public_directory: self.3.public_directory.clone(),
        })
    }
    pub(crate) fn persist_session_image(
        &self,
        data: &str,
        mime_type: &str,
    ) -> Result<Option<super::Artifact>, String> {
        let bytes = super::decode_image_data(data)?;
        Ok(self.persist_decoded_image(None, &bytes, mime_type, data.len()))
    }
}
