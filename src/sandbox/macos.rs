use std::ffi::OsString;
use std::fs::{self, DirBuilder};
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, ExitStatus};
use std::time::{SystemTime, UNIX_EPOCH};

pub(super) const SANDBOX_EXEC: &str = "/usr/bin/sandbox-exec";
const POLICY: &str = include_str!("read_only_policy.sbpl");

pub(super) fn sandboxed_command(
    writable_directory: Option<&Path>,
) -> Result<(Command, TemporaryDirectory), String> {
    let temporary_directory = TemporaryDirectory::new()?;
    let writable_directory = writable_directory.unwrap_or_else(|| temporary_directory.path());
    let mut launcher = Command::new(SANDBOX_EXEC);
    launcher
        .arg("-p")
        .arg(POLICY)
        .arg(parameter_definition(
            "TEMP_DIRECTORY",
            temporary_directory.path(),
        ))
        .arg(parameter_definition(
            "EXTRA_WRITABLE_DIRECTORY",
            writable_directory,
        ))
        .arg("--");

    Ok((launcher, temporary_directory))
}

pub(crate) struct TemporaryDirectory(PathBuf);

impl TemporaryDirectory {
    pub(crate) fn new() -> Result<Self, String> {
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

    pub(crate) fn path(&self) -> &Path {
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

pub(super) fn exit_code(status: ExitStatus) -> ExitCode {
    let code = status
        .code()
        .unwrap_or_else(|| 128 + status.signal().unwrap_or(1));
    ExitCode::from(code as u8)
}
