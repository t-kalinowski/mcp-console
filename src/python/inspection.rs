use std::ffi::OsString;
use std::fs::{self, File};
use std::os::fd::FromRawFd as _;
use std::os::unix::ffi::{OsStrExt as _, OsStringExt as _};
use std::path::{Path, PathBuf};
use std::process::Stdio;

use crate::resolver::ResolverStopHandle;
use crate::resolver::process::{ResolverProcess, completed_write, resolver_command};

use super::startup::SelectedPython;

const INSPECTION_SOURCE: &str = include_str!("inspection.py");

/// Executable and environment identity observed together on the host.
#[derive(Clone, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct NativePython {
    pub(crate) embedding: SelectedPython,
    pub(crate) prefix: String,
    pub(crate) exec_prefix: String,
    pub(crate) base_prefix: String,
    pub(crate) base_exec_prefix: String,
}

/// Describe a selected executable without changing the calling process or
/// selecting a replacement. The selected installation is trusted and must
/// remain stable through initialization; concurrent replacement is unsupported.
pub(crate) fn inspect_selected(
    executable: &Path,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<SelectedPython, String> {
    inspect_native(executable, on_started).map(|selected| selected.embedding)
}

pub(crate) fn inspect_native(
    executable: &Path,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<NativePython, String> {
    inspect_prepared(executable, None, on_started)
}

pub(crate) fn inspect_prepared(
    executable: &Path,
    preparation: Option<&crate::resolver::ManagedPythonResolverConfiguration>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<NativePython, String> {
    if !executable.is_absolute() || !executable.is_file() {
        return Err(format!(
            "selected Python executable is not an absolute file: {}",
            executable.display()
        ));
    }
    let selected = executable
        .to_str()
        .ok_or_else(|| "selected Python executable is not UTF-8".to_string())?;
    let directory = preparation.map_or_else(std::env::temp_dir, |configuration| {
        configuration.output_directory()
    });
    let result = InspectionOutput::create(&directory)?;
    let resolver = ResolverProcess::for_preparation(
        preparation.and_then(|configuration| configuration.preparation_directory()),
    )?;
    let mut command = preparation.map_or_else(
        || resolver_command(executable),
        |configuration| configuration.command(executable, &resolver),
    );
    command
        // Inspect the selected installation without executing workspace,
        // PYTHONPATH, or user-site code with the host resolver's permissions.
        .arg("-I")
        .arg("-c")
        .arg(INSPECTION_SOURCE)
        .arg(result.path())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let (mut child, [stdout, stderr]) = resolver.spawn(&mut command).map_err(|error| {
        format!("failed to inspect selected Python executable `{selected}`: {error}")
    })?;
    if let Err(error) = on_started(resolver.stop_handle()) {
        resolver
            .abort(&mut child, executable, "Python inspection")
            .map_err(|cleanup| format!("{error}; {cleanup}"))?;
        return Err(error);
    }
    let output = resolver.wait(
        &mut child,
        completed_write(),
        stdout,
        stderr,
        executable,
        "Python inspection",
    )?;
    output.write_result.map_err(|error| error.to_string())?;
    if !output.status.success() {
        let diagnostic = String::from_utf8_lossy(&output.stderr);
        let ordinary = String::from_utf8_lossy(&output.stdout);
        return Err(format!(
            "selected Python inspection failed ({}): {}{}",
            output.status, ordinary, diagnostic
        ));
    }
    let description: Description = serde_json::from_slice(
        &fs::read(result.path())
            .map_err(|error| format!("failed to read selected Python configuration: {error}"))?,
    )
    .map_err(|error| format!("invalid selected Python configuration: {error}"))?;
    description.validate(executable)?;
    if let Some(configuration) = preparation {
        for path in [
            selected,
            &description.libpython,
            &description.prefix,
            &description.exec_prefix,
            &description.base_prefix,
            &description.base_exec_prefix,
        ] {
            configuration.ensure_safe_python_path(Path::new(path))?;
        }
    }
    let python_home = if description.base_prefix == description.base_exec_prefix {
        description.base_prefix.clone()
    } else {
        format!(
            "{}:{}",
            description.base_prefix, description.base_exec_prefix
        )
    };
    Ok(NativePython {
        embedding: SelectedPython {
            // sys.executable verifies the child's identity, while the caller's
            // spelling retains a selected virtualenv or other executable symlink.
            python: selected.to_string(),
            libpython: description.libpython,
            python_home,
        },
        prefix: description.prefix,
        exec_prefix: description.exec_prefix,
        base_prefix: description.base_prefix,
        base_exec_prefix: description.base_exec_prefix,
    })
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Description {
    executable: PathBuf,
    libpython: String,
    prefix: String,
    exec_prefix: String,
    base_prefix: String,
    base_exec_prefix: String,
}

impl Description {
    fn validate(&self, selected: &Path) -> Result<(), String> {
        let reported = fs::canonicalize(&self.executable).map_err(|error| {
            format!(
                "selected Python reported unusable executable `{}`: {error}",
                self.executable.display()
            )
        })?;
        let selected_identity = fs::canonicalize(selected).map_err(|error| {
            format!("selected Python executable disappeared during inspection: {error}")
        })?;
        if !self.executable.is_absolute() || reported != selected_identity {
            return Err(format!(
                "selected Python reported a different executable: {}",
                self.executable.display()
            ));
        }
        for (kind, path) in [
            ("prefix", &self.prefix),
            ("exec prefix", &self.exec_prefix),
            ("base prefix", &self.base_prefix),
            ("base exec prefix", &self.base_exec_prefix),
        ] {
            if !Path::new(path).is_absolute() || !Path::new(path).is_dir() {
                return Err(format!("selected Python returned invalid {kind}: {path}"));
            }
        }
        let library = Path::new(&self.libpython);
        if !library.is_absolute() || !library.is_file() {
            return Err(format!(
                "selected Python embedding library is missing: {}",
                library.display()
            ));
        }
        Ok(())
    }
}

struct InspectionOutput(PathBuf);

impl InspectionOutput {
    fn create(directory: &Path) -> Result<Self, String> {
        let mut template = directory
            .join("mcp-console-python-inspection-XXXXXX")
            .as_os_str()
            .as_bytes()
            .to_vec();
        template.push(0);
        // SAFETY: mkstemp replaces the trailing Xs and returns a new owned fd.
        let descriptor = unsafe { libc::mkstemp(template.as_mut_ptr().cast()) };
        if descriptor < 0 {
            return Err(format!(
                "failed to create Python inspection output: {}",
                std::io::Error::last_os_error()
            ));
        }
        // SAFETY: the descriptor is uniquely owned here. The child opens the
        // path itself, so close this descriptor before launching it.
        drop(unsafe { File::from_raw_fd(descriptor) });
        let path = OsString::from_vec(template[..template.len() - 1].to_vec());
        Ok(Self(PathBuf::from(path)))
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for InspectionOutput {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}
