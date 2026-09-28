//! CPython bootstrap operations, independent of the R/reticulate adapter.
//! Interpreter lifetime and setup completion are retained by `library`.

use super::ImportResolution;
use std::path::Path;

#[derive(Clone, Debug, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SelectedPython {
    pub(crate) python: String,
    pub(crate) libpython: String,
    pub(crate) python_home: String,
}

pub(crate) fn initialize_selected(selected: &SelectedPython) -> Result<bool, String> {
    super::library::initialize(
        Path::new(&selected.libpython),
        &selected.python,
        &selected.python_home,
        true,
    )
}

pub(super) fn install_services(libpython: &Path) -> Result<(), String> {
    // Reticulate calls this after its own stream and input hooks. Reasserting
    // the interrupt handler is intentional when those hooks run again.
    super::library::load(libpython)?;
    super::library::install_services()
}

/// Install the shared evaluator after the selected interpreter is live.
/// Reticulate may call this from its initialization hook, while a native
/// caller can pass no callback without constructing an R adapter.
/// Successful steps remain in the process-lifetime library state, so a later
/// call resumes incomplete setup without replacing the interpreter.
pub(crate) fn setup_runtime(
    libpython: &Path,
    resolution: ImportResolution<'_>,
) -> Result<bool, String> {
    setup_runtime_with_sql(libpython, resolution, true)
}

fn setup_runtime_with_sql(
    libpython: &Path,
    resolution: ImportResolution<'_>,
    sql: bool,
) -> Result<bool, String> {
    super::library::load(libpython)?;
    if super::library::runtime_configured()? {
        return Ok(true);
    }
    // The hook may already have installed services. Native callers arrive
    // here directly, and perform the same installation once.
    if !super::library::services_installed()? {
        install_services(libpython)?;
    }
    super::library::install_runtime(super::RUNTIME_SOURCE)?;
    if sql {
        crate::sql::install_python_runtime()?;
    }
    // The finder starts with automatic resolution disabled. A native caller
    // with neither input keeps that default rather than replacing its reason
    // with Python None.
    if (resolution.callback.is_some() || resolution.disabled_reason.is_some())
        && !super::library::configure_import_resolution(resolution)?
    {
        return Ok(false);
    }
    super::library::mark_runtime_configured(sql)?;
    Ok(true)
}

pub(crate) fn finish_initialization() -> Result<(), String> {
    super::library::finish_initialization()
}

/// Initialize a host-selected interpreter without constructing an R adapter.
/// Successful return includes environment, SQL, and import-resolution setup.
pub(super) fn initialize_native(
    configuration: &super::NativePython,
    managed: bool,
) -> Result<(), String> {
    let selected = &configuration.embedding;
    // Let CPython's program-name/pyvenv.cfg path initialization select the
    // virtualenv. Setting PythonHome to its base overrides that selection.
    super::library::initialize(Path::new(&selected.libpython), &selected.python, "", false)?;
    let result = setup_runtime_with_sql(
        Path::new(&selected.libpython),
        ImportResolution {
            callback: None,
            disabled_reason: Some(if managed {
                crate::local_runtime::MANAGED_IMPORT_DISABLED
            } else {
                match std::env::var("MCP_CONSOLE_EXECUTION_COMPUTE").as_deref() {
                    Ok("docker") => "automatic package installation is unavailable in prepared Docker targets; preinstall the distribution in the image and start a new server session",
                    Ok("docker_sandbox") => "automatic package installation is unavailable in prepared Docker Sandbox targets; preinstall the distribution in the template and start a new server session",
                    _ => crate::local_runtime::IMPORT_DISABLED,
                }
            }),
        },
        true,
    )
    .and_then(|configured| {
        if !configured {
            return Err("native Python setup did not complete".into());
        }
        super::library::configure_native_environment(configuration)?;
        super::library::configure_native_sql()?;
        if !super::library::disable_matplotlib_show()? {
            return Err("Python plotting setup did not complete".into());
        }
        Ok(())
    });
    if result.is_err() {
        super::library::display_setup_exception()?;
    }
    let finished = finish_initialization();
    result?;
    finished?;
    if managed {
        let manifest = std::env::var("MCP_CONSOLE_MANAGED_PYTHON")
            .map_err(|error| format!("managed Python launch omitted its declaration: {error}"))?;
        let manifest = serde_json::from_str(&manifest)
            .map_err(|error| format!("invalid managed Python declaration: {error}"))?;
        super::native::initialize(configuration, manifest)?;
        super::library::configure_native_import_resolution()?;
    }
    Ok(())
}
