use std::ffi::OsString;
use std::fs::{self, DirBuilder};
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, ExitStatus};
use std::time::{SystemTime, UNIX_EPOCH};

const SANDBOX_EXEC: &str = "/usr/bin/sandbox-exec";
const POLICY: &str = include_str!("read_only_policy.sbpl");

pub(super) fn sandboxed_command() -> Result<(Command, TemporaryDirectory, OsString), String> {
    let temporary_directory = TemporaryDirectory::new()?;
    let temporary_directory_path = temporary_directory.path().as_os_str().to_os_string();
    let mut launcher = Command::new(SANDBOX_EXEC);
    launcher
        .arg("-p")
        .arg(POLICY)
        .arg(parameter_definition(
            "TEMP_DIRECTORY",
            temporary_directory.path(),
        ))
        .arg("--");

    Ok((launcher, temporary_directory, temporary_directory_path))
}

pub(super) fn status(launcher: &mut Command) -> Result<ExitCode, String> {
    // This initial launcher intentionally waits only for the direct command.
    // Descendant cleanup is deferred because it must handle process groups,
    // children that create new sessions, signal forwarding, and PID reuse as
    // one lifecycle boundary. Background descendants are unsupported: they may
    // outlive the launcher, which attempts to remove this directory on return.
    let status = launcher
        .status()
        .map_err(|error| format!("failed to launch `{SANDBOX_EXEC}`: {error}"))?;

    Ok(exit_code(status))
}

pub(super) struct TemporaryDirectory(PathBuf);

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

        let mut directory = Self(path);
        directory.0 = directory.0.canonicalize().map_err(|error| {
            format!(
                "failed to resolve temporary directory `{}`: {error}",
                directory.0.display()
            )
        })?;
        Ok(directory)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TemporaryDirectory {
    fn drop(&mut self) {
        // Cleanup must not replace the child status if it changed directory modes.
        let _ = fs::remove_dir_all(&self.0);
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

fn exit_code(status: ExitStatus) -> ExitCode {
    let code = status
        .code()
        .unwrap_or_else(|| 128 + status.signal().unwrap_or(1));
    ExitCode::from(code as u8)
}
