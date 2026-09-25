use std::ffi::OsString;
use std::fs::{self, File};
use std::os::fd::FromRawFd as _;
use std::os::unix::ffi::{OsStrExt as _, OsStringExt as _};
use std::os::unix::fs::MetadataExt as _;
use std::path::{Path, PathBuf};
use std::process::Stdio;

use crate::resolver::ResolverStopHandle;
use crate::resolver::process::{ResolverProcess, completed_write, read_output, resolver_command};

use super::startup::SelectedPython;

const INSPECTION_SOURCE: &str = include_str!("inspection.py");

/// Describe a selected executable without changing the calling process or
/// selecting a replacement. Ordinary reticulate startup already supplies its
/// configuration and does not call this function.
pub(crate) fn inspect_selected(
    executable: &Path,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<SelectedPython, String> {
    if !executable.is_absolute() || !executable.is_file() {
        return Err(format!(
            "selected Python executable is not an absolute file: {}",
            executable.display()
        ));
    }
    let selected = executable
        .to_str()
        .ok_or_else(|| "selected Python executable is not UTF-8".to_string())?;
    let identity = ExecutableIdentity::capture(executable)?;
    let image_identity = ExecutableIdentity::capture_image(executable)?;
    let result = InspectionOutput::create()?;
    let resolver = ResolverProcess::new();
    let mut command = resolver_command(executable);
    command
        .arg("-c")
        .arg(INSPECTION_SOURCE)
        .arg(result.path())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn().map_err(|error| {
        format!("failed to inspect selected Python executable `{selected}`: {error}")
    })?;
    let stdout = read_output(child.stdout.take().expect("inspection stdout is piped"));
    let stderr = read_output(child.stderr.take().expect("inspection stderr is piped"));
    resolver.watch_exit(child.id());
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
    description.validate(executable, identity, image_identity)?;
    let python_home = if description.base_prefix == description.base_exec_prefix {
        description.base_prefix.clone()
    } else {
        format!(
            "{}:{}",
            description.base_prefix, description.base_exec_prefix
        )
    };
    Ok(SelectedPython {
        // sys.executable verifies the child's identity, while the caller's
        // spelling retains a selected virtualenv or other executable symlink.
        python: selected.to_string(),
        libpython: description.libpython,
        python_home,
    })
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Description {
    executable: PathBuf,
    image_identity: [u64; 2],
    libpython: String,
    prefix: String,
    exec_prefix: String,
    base_prefix: String,
    base_exec_prefix: String,
}

#[derive(PartialEq)]
struct ExecutableIdentity {
    device: u64,
    inode: u64,
}

impl ExecutableIdentity {
    fn capture(path: &Path) -> Result<Self, String> {
        let metadata = fs::metadata(path).map_err(|error| {
            format!("failed to inspect selected Python executable identity: {error}")
        })?;
        Ok(Self {
            device: metadata.dev(),
            inode: metadata.ino(),
        })
    }

    fn capture_image(path: &Path) -> Result<Self, String> {
        #[cfg(target_os = "macos")]
        {
            // Framework bin launchers can exec Resources/Python.app while
            // preserving their bin path as sys.executable.
            let resolved = fs::canonicalize(path).map_err(|error| {
                format!("failed to resolve selected Python executable: {error}")
            })?;
            let bin = resolved.parent();
            let version = bin.and_then(Path::parent);
            let versions = version.and_then(Path::parent);
            let framework = versions.and_then(Path::parent);
            if bin.is_some_and(|part| part.file_name().is_some_and(|name| name == "bin"))
                && versions
                    .is_some_and(|part| part.file_name().is_some_and(|name| name == "Versions"))
                && framework.is_some_and(|part| {
                    part.file_name()
                        .and_then(|name| name.to_str())
                        .is_some_and(|name| name.ends_with(".framework"))
                })
            {
                let image = version
                    .expect("framework version was found")
                    .join("Resources/Python.app/Contents/MacOS/Python");
                return Self::capture(&image);
            }
        }
        Self::capture(path)
    }
}

impl Description {
    fn validate(
        &self,
        selected: &Path,
        identity: ExecutableIdentity,
        image_identity: ExecutableIdentity,
    ) -> Result<(), String> {
        if ExecutableIdentity::capture(selected)? != identity {
            return Err("selected Python executable changed during inspection".to_string());
        }
        if self.image_identity != [image_identity.device, image_identity.inode] {
            return Err("selected Python executable image differs from selected file".to_string());
        }
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
    fn create() -> Result<Self, String> {
        let mut template = std::env::temp_dir()
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
