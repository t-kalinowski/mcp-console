use std::ffi::OsString;
use std::fs::{self, DirBuilder};
use std::io;
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, ExitStatus};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub(super) const SANDBOX_EXEC: &str = "/usr/bin/sandbox-exec";
const POLICY: &str = include_str!("read_only_policy.sbpl");
pub(super) fn wait_for_process_exit_without_reaping(
    process_id: u32,
    timeout: Duration,
) -> io::Result<bool> {
    crate::process_exit::wait_for_process_exit_without_reaping(process_id, timeout)
}

pub(super) fn kill_process_group(process_group_id: u32) -> io::Result<()> {
    crate::process_group::kill(process_group_id)
}

pub(super) fn sandboxed_command() -> Result<(Command, TemporaryDirectory), String> {
    let temporary_directory = TemporaryDirectory::new()?;
    let mut launcher = Command::new(SANDBOX_EXEC);
    // Do not rely on SIP to keep host interposers out of the Apple sandbox
    // intermediary and the sandbox target.
    launcher
        .env_remove("DYLD_INSERT_LIBRARIES")
        .arg("-p")
        .arg(POLICY)
        .arg(parameter_definition(
            "TEMP_DIRECTORY",
            temporary_directory.path(),
        ))
        .arg("--");

    Ok((launcher, temporary_directory))
}

pub(crate) struct TemporaryDirectory {
    path: PathBuf,
    remove_on_drop: bool,
}

impl TemporaryDirectory {
    fn new() -> Result<Self, String> {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|error| format!("failed to read the system clock: {error}"))?
            .as_nanos();
        let path =
            std::env::temp_dir().join(format!("mcp-console-tmp-{}-{unique}", std::process::id()));

        DirBuilder::new()
            .mode(0o700)
            .create(&path)
            .map_err(|error| {
                format!(
                    "failed to create temporary directory `{}`: {error}",
                    path.display()
                )
            })?;

        let mut temporary_directory = Self {
            path,
            remove_on_drop: true,
        };
        temporary_directory.path = temporary_directory.path.canonicalize().map_err(|error| {
            format!(
                "failed to resolve temporary directory `{}`: {error}",
                temporary_directory.path.display()
            )
        })?;
        Ok(temporary_directory)
    }

    pub(super) fn path(&self) -> &Path {
        &self.path
    }

    pub(crate) fn adopt(path: PathBuf, owner_pid: libc::pid_t) -> Result<Self, String> {
        if owner_pid <= 0 {
            return Err("sandbox temporary directory has invalid ownership".to_string());
        }

        let path = path.canonicalize().map_err(|error| {
            format!(
                "failed to resolve sandbox temporary directory {}: {error}",
                path.display()
            )
        })?;
        let expected_prefix = format!("mcp-console-tmp-{owner_pid}-");
        let valid_name = path
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.starts_with(&expected_prefix));
        let expected_parent = std::env::temp_dir().canonicalize().map_err(|error| {
            format!("failed to resolve the system temporary directory: {error}")
        })?;
        if !valid_name || path.parent() != Some(expected_parent.as_path()) {
            return Err("sandbox temporary directory has invalid ownership".to_string());
        }
        if !path.is_dir() {
            return Err(format!(
                "sandbox temporary directory {} is not a directory",
                path.display()
            ));
        }
        Ok(Self {
            path,
            // The adopting manager must prove cleanup before removing the
            // directory. Preserve it if the manager unwinds unexpectedly.
            remove_on_drop: false,
        })
    }

    /// Leaves the directory in place because cleanup could not prove that it is unused.
    pub(crate) fn preserve(mut self) {
        self.remove_on_drop = false;
    }

    /// Arms best-effort removal after cleanup proved that the directory is unused.
    pub(crate) fn remove(mut self) {
        self.remove_on_drop = true;
    }

    /// Transfers cleanup ownership to another guard for the same directory.
    pub(crate) fn relinquish(&mut self) {
        self.remove_on_drop = false;
    }
}

impl Drop for TemporaryDirectory {
    fn drop(&mut self) {
        if self.remove_on_drop {
            // Cleanup must not replace the child status if it changed directory modes.
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}

// `sandbox-exec -DNAME=VALUE` supplies values for `(param "NAME")` in the SBPL.
fn parameter_definition(name: &str, path: &Path) -> OsString {
    let mut argument = OsString::from("-D");
    argument.push(name);
    argument.push("=");
    argument.push(path);
    argument
}

pub(super) fn exit_code(status: ExitStatus) -> ExitCode {
    let code = status
        .code()
        .unwrap_or_else(|| 128 + status.signal().unwrap_or(1));
    ExitCode::from(code as u8)
}
