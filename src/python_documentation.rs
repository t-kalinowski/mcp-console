//! Explicit host administration, separate from worker/resolver preparation.
use std::path::Path;
use std::process::{Command, ExitCode};

pub(crate) const LOOKUP_SOURCE: &str = include_str!("python/documentation.py");

pub(crate) fn prepare(
    python: &Path,
    archive: Option<&Path>,
    download_page: Option<&Path>,
) -> Result<ExitCode, String> {
    let source = format!(
        "{LOOKUP_SOURCE}\n{}",
        include_str!("python/prepare_documentation.py")
    );
    let mut command = Command::new(python);
    // Trusted executable selection is explicit; ignore Python startup hooks.
    command.args(["-I", "-c", &source]);
    if let Some(archive) = archive {
        command.arg("--archive").arg(archive);
    }
    if let Some(page) = download_page {
        command.arg("--download-page").arg(page);
    }
    command
        .status()
        .map(|status| {
            if status.success() {
                ExitCode::SUCCESS
            } else {
                ExitCode::FAILURE
            }
        })
        .map_err(|error| format!("cannot run documentation preparation Python: {error}"))
}
