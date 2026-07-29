use std::ffi::OsString;
use std::fs::{self, DirBuilder};
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, ExitStatus};
use std::time::{SystemTime, UNIX_EPOCH};

const SANDBOX_EXEC: &str = "/usr/bin/sandbox-exec";
const ENV: &str = "/usr/bin/env";
const POLICY: &str = include_str!("read_only_policy.sbpl");

pub(super) fn run(command: &[OsString]) -> Result<ExitCode, String> {
    let temp_directory = TemporaryDirectory::new()?;

    // This initial launcher intentionally waits only for the direct command.
    // Descendant cleanup is deferred because it must handle process groups,
    // children that create new sessions, signal forwarding, and PID reuse as
    // one lifecycle boundary. Background descendants are unsupported: they may
    // outlive the launcher, which attempts to remove this directory on return.
    let status = Command::new(SANDBOX_EXEC)
        .arg("-p")
        .arg(POLICY)
        .arg(parameter_definition(
            "TEMP_DIRECTORY",
            temp_directory.path(),
        ))
        .arg("--")
        .args(command)
        .env("TMPDIR", temp_directory.path())
        .status()
        .map_err(|error| format!("failed to launch `{SANDBOX_EXEC}`: {error}"))?;

    Ok(exit_code(status))
}

pub(super) fn worker_command() -> Result<(Command, TemporaryDirectory), String> {
    let temporary_directory = TemporaryDirectory::new()?;
    let mut command = Command::new(SANDBOX_EXEC);
    command
        .arg("-p")
        .arg(POLICY)
        .arg(parameter_definition(
            "TEMP_DIRECTORY",
            temporary_directory.path(),
        ))
        .arg("--")
        // sandbox-exec removes DYLD_* variables before launching its child.
        // Applying explicit worker variables through env restores them inside
        // the sandbox without invoking a shell.
        .arg(ENV)
        .arg(environment_assignment("TMPDIR", temporary_directory.path()));

    Ok((command, temporary_directory))
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

fn environment_assignment(name: &str, path: &Path) -> OsString {
    let mut argument = OsString::from(name);
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
