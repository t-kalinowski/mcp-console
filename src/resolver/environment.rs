//! Retained environment descriptions; package preparation is platform-specific.
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Clone, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ManagedPython {
    pub(super) python: PathBuf,
    pub(super) requirements: crate::worker_protocol::PythonRequirementManifest,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(super) native: Option<Box<crate::python::NativePython>>,
}

impl ManagedPython {
    pub(crate) fn native(&self) -> Option<&crate::python::NativePython> {
        self.native.as_deref()
    }
    pub(crate) fn set_native(&mut self, native: crate::python::NativePython) {
        self.native = Some(Box::new(native));
    }

    pub(crate) fn configure_worker(&self, command: &mut Command) {
        command.env("RETICULATE_PYTHON", "managed");
        command.env(
            "MCP_CONSOLE_MANAGED_PYTHON",
            serde_json::to_string(&self.requirements)
                .expect("managed Python requirements should serialize as JSON"),
        );
    }

    pub(crate) fn python(&self) -> &Path {
        &self.python
    }

    pub(crate) fn requirements(&self) -> &crate::worker_protocol::PythonRequirementManifest {
        &self.requirements
    }

    pub(crate) fn with_retained_requirements(
        mut self,
        requirements: crate::worker_protocol::PythonRequirementManifest,
    ) -> Self {
        self.requirements = requirements;
        self
    }
}
#[derive(Clone, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ManagedR {
    pub(super) library: PathBuf,
    // Serde's Unix OsString representation preserves native path bytes in JSON.
    pub(super) r_libs: OsString,
    // Executable selection never travels in a preparation request.
    #[cfg(any(unix, windows))]
    #[serde(skip)]
    pub(super) rscript: PathBuf,
    pub(super) requirements: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(super) extension_directory: Option<PathBuf>,
}
impl ManagedR {
    #[cfg(any(unix, windows))]
    pub(crate) fn on_host(mut self, rscript: &Path) -> Self {
        self.rscript = rscript.to_path_buf();
        self
    }
    pub(crate) fn configure_worker(&self, command: &mut Command) -> Result<(), String> {
        if !self.library.is_dir() {
            return Err(format!(
                "resolved R library `{}` no longer exists",
                self.library.display()
            ));
        }
        // Keep the accepted library first and preserve ambient worker-library
        // precedence before resolver bootstrap libraries.
        let mut libraries = vec![self.library.clone()];
        if let Some(inherited) = std::env::var_os("R_LIBS") {
            for path in std::env::split_paths(&inherited) {
                if !path.as_os_str().is_empty() && !libraries.contains(&path) {
                    libraries.push(path);
                }
            }
        }
        for path in self.library_paths() {
            if !libraries.contains(&path) {
                libraries.push(path);
            }
        }
        command.env(
            "R_LIBS",
            std::env::join_paths(libraries).map_err(|e| e.to_string())?,
        );
        if let Some(directory) = &self.extension_directory {
            command.env("MCP_CONSOLE_EXTENSION_DIRECTORY", directory);
        }
        Ok(())
    }

    pub(crate) fn with_retained_requirements(mut self, requirements: Vec<String>) -> Self {
        self.requirements = requirements;
        self
    }

    pub(crate) fn requirements(&self) -> &[String] {
        &self.requirements
    }

    pub(crate) fn library(&self) -> &Path {
        &self.library
    }

    pub(crate) fn library_paths(&self) -> impl Iterator<Item = PathBuf> + '_ {
        std::env::split_paths(&self.r_libs)
    }
    pub(crate) fn extension_directory(&self) -> Option<&Path> {
        self.extension_directory.as_deref()
    }
    #[cfg(any(unix, windows))]
    pub(crate) fn rscript(&self) -> &Path {
        &self.rscript
    }
}
