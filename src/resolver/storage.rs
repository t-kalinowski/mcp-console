//! One owned payload tree and protected, process-coordinated session leases.

use std::fs::{self, File, OpenOptions};
use std::io::{Seek, SeekFrom, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

const WEEK: u64 = 7 * 24 * 60 * 60;

#[derive(Default, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct Metadata {
    cleaned_at: u64,
    // Diagnostic count; kernel locks, including launcher locks, own lifetime.
    leases: u64,
    #[serde(default)]
    uncertain: bool,
}

pub(super) struct Storage {
    pub(super) root: PathBuf,
    pub(super) payload: PathBuf,
    gate: File,
    lease: File,
}

fn directory(path: &Path) -> Result<(), String> {
    match fs::DirBuilder::new().mode(0o700).create(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            let metadata = fs::symlink_metadata(path).map_err(|error| error.to_string())?;
            if !metadata.is_dir() {
                return Err(format!(
                    "resolver storage must be a directory: {}",
                    path.display()
                ));
            }
            Ok(())
        }
        Err(error) => Err(format!(
            "cannot create resolver storage {}: {error}",
            path.display()
        )),
    }
}

fn open(path: &Path) -> Result<File, String> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|error| {
            format!(
                "cannot open resolver control file {}: {error}",
                path.display()
            )
        })?;
    if !file
        .metadata()
        .map_err(|error| error.to_string())?
        .is_file()
    {
        return Err("resolver control file is not regular".into());
    }
    Ok(file)
}

fn lock(file: &File, operation: i32) -> Result<(), String> {
    loop {
        if unsafe { libc::flock(file.as_raw_fd(), operation) } == 0 {
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(format!("resolver lease lock failed: {error}"));
        }
    }
}

impl Storage {
    pub(super) fn acquire(cache: &Path, trusted: &[&Path]) -> Result<Self, String> {
        if !cache.is_absolute() {
            return Err("resolver cache home must be absolute".into());
        }
        fs::create_dir_all(cache).map_err(|error| error.to_string())?;
        let cache = cache.canonicalize().map_err(|error| error.to_string())?;
        let parent = cache.join("mcp-console");
        directory(&parent)?;
        let root = parent.join("resolver");
        directory(&root)?;
        if trusted.iter().any(|path| path.starts_with(&root)) {
            return Err(
                "Console and its native runner must be installed outside resolver storage".into(),
            );
        }
        let control = root.join("control");
        directory(&control)?;
        let mut storage = Self {
            payload: root.join("payload"),
            gate: open(&control.join("lock"))?,
            lease: open(&control.join("leases"))?,
            root,
        };
        lock(&storage.gate, libc::LOCK_EX)?;
        let mut metadata = storage.read_metadata()?;
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|e| e.to_string())?
            .as_secs();
        let unused = loop {
            if unsafe { libc::flock(storage.lease.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0
            {
                break true;
            }
            let error = std::io::Error::last_os_error();
            match error.kind() {
                std::io::ErrorKind::Interrupted => continue,
                std::io::ErrorKind::WouldBlock => break false,
                _ => return Err(error.to_string()),
            }
        };
        if unused && !metadata.uncertain && now.saturating_sub(metadata.cleaned_at) >= WEEK {
            // remove_dir_all does not follow symlinks, including the root.
            // The exclusive lease excludes writers; metadata is outside payload.
            match fs::remove_dir_all(&storage.payload) {
                Ok(()) => (),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
                Err(error) => return Err(format!("resolver cleanup failed: {error}")),
            }
            directory(&storage.payload)?;
            metadata.cleaned_at = now;
        }
        if unused {
            metadata.leases = 0;
        }
        directory(&storage.payload)?;
        lock(&storage.lease, libc::LOCK_SH)?;
        metadata.leases = metadata
            .leases
            .checked_add(1)
            .ok_or("resolver lease count overflow")?;
        storage.write_metadata(&metadata)?;
        lock(&storage.gate, libc::LOCK_UN)?;
        Ok(storage)
    }

    fn read_metadata(&mut self) -> Result<Metadata, String> {
        let path = self.root.join("control/metadata.json");
        match fs::read(&path) {
            Ok(bytes) => serde_json::from_slice(&bytes)
                .map_err(|e| format!("invalid resolver cleanup metadata: {e}")),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Metadata::default()),
            Err(error) => Err(error.to_string()),
        }
    }

    fn write_metadata(&self, metadata: &Metadata) -> Result<(), String> {
        let temporary = self.root.join("control/metadata.new");
        let mut file = open(&temporary)?;
        file.set_len(0).map_err(|e| e.to_string())?;
        file.seek(SeekFrom::Start(0)).map_err(|e| e.to_string())?;
        file.write_all(&serde_json::to_vec(metadata).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
        file.sync_all().map_err(|e| e.to_string())?;
        fs::rename(temporary, self.root.join("control/metadata.json")).map_err(|e| e.to_string())
    }

    pub(super) fn lease_path(&self) -> PathBuf {
        self.root.join("control/leases")
    }

    pub(super) fn quarantine(mut self) -> Result<(), String> {
        lock(&self.gate, libc::LOCK_EX)?;
        let mut metadata = self.read_metadata()?;
        metadata.uncertain = true;
        self.write_metadata(&metadata)?;
        lock(&self.gate, libc::LOCK_UN)
    }

    pub(super) fn release(mut self) -> Result<(), String> {
        lock(&self.gate, libc::LOCK_EX)?;
        let mut metadata = self.read_metadata()?;
        metadata.leases = metadata
            .leases
            .checked_sub(1)
            .ok_or("resolver lease underflow")?;
        self.write_metadata(&metadata)?;
        lock(&self.lease, libc::LOCK_UN)?;
        lock(&self.gate, libc::LOCK_UN)
    }
}

/// Retain an independent shared lock in the trusted native launcher. Its
/// descriptor sweep marks the lock close-on-exec before starting any workload.
/// Register after the caller's ordinary descriptor sweep.
pub(crate) fn inherit_lease(
    command: &mut std::process::Command,
    path: &Path,
) -> Result<(), String> {
    let lease = File::open(path).map_err(|e| e.to_string())?;
    lock(&lease, libc::LOCK_SH)?;
    unsafe {
        command.pre_exec(move || {
            if libc::fcntl(lease.as_raw_fd(), libc::F_SETFD, 0) < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    Ok(())
}
