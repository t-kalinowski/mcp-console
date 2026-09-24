use libr::SEXP;

use super::PreparationOutcome;

const PYTHON_BRIDGE_SOURCE: &str = include_str!("bridge.R");
const PYTHON_INITIALIZER_SOURCE: &str = include_str!("initialize.R");

/// Reticulate supplies the selected configuration and attaches to the native interpreter.
pub(super) struct Adapter {
    bridge: crate::r_bridge::Bridge,
}

#[derive(serde::Deserialize)]
pub(super) struct SelectedPython {
    pub(super) python: String,
    pub(super) libpython: String,
    pub(super) python_home: String,
}

pub(super) fn configure_worker_environment() -> std::io::Result<()> {
    super::platform::set_environment(c"RETICULATE_REMAP_OUTPUT_STREAMS", c"0", true)
}

impl Adapter {
    pub(super) fn initialize() -> Result<Self, String> {
        let source = format!(
            "base::local(\n  {{\n    state <- ({PYTHON_BRIDGE_SOURCE})\n{PYTHON_INITIALIZER_SOURCE}\n    state\n  }},\n  envir = base::new.env(parent = base::baseenv())\n)"
        );
        Ok(Self {
            bridge: crate::r_bridge::Bridge::initialize(&source, "Python")?,
        })
    }

    pub(super) fn select(&mut self) -> Result<Option<SelectedPython>, String> {
        // Discovery and serialization share the existing R interrupt boundary.
        self.bridge
            .evaluate_completed_string("select")?
            .map(|selected| {
                serde_json::from_str(&selected)
                    .map_err(|error| format!("invalid selected Python configuration: {error}"))
            })
            .transpose()
    }

    pub(super) fn cancel_selection(&self) -> Result<(), String> {
        self.bridge.call0_string(c"cancel_python_selection")?;
        Ok(())
    }

    pub(super) fn attach_and_setup(&mut self) -> Result<bool, String> {
        self.bridge.evaluate_completed("")
    }

    pub(super) fn prepare(&self, packages: Vec<String>) -> Result<PreparationOutcome, String> {
        let request = serde_json::to_string(&packages)
            .map_err(|error| format!("failed to serialize Python preparation: {error}"))?;
        let response = self
            .bridge
            .call1_string(c"prepare", &request)?
            .ok_or_else(|| "Python preparation bridge returned no response".to_string())?;
        serde_json::from_str(&response)
            .map_err(|error| format!("invalid Python preparation response: {error}"))
    }
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_resolve_python(request: SEXP) -> harp::Result<SEXP> {
    let request = String::try_from(harp::object::RObject::view(request))?;
    let request = serde_json::from_str(&request).map_err(|error| harp::anyhow!("{error}"))?;
    let python =
        crate::worker::resolve_python(request).map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(python).sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_resolve_python_version(request: SEXP) -> harp::Result<SEXP> {
    let request = String::try_from(harp::object::RObject::view(request))?;
    let request = serde_json::from_str(&request).map_err(|error| harp::anyhow!("{error}"))?;
    let version =
        crate::worker::resolve_python_version(request).map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(version).sexp)
}
