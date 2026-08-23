const R_ENVIRONMENT_BRIDGE_SOURCE: &str = include_str!("r_environment/bridge.R");
const ENABLE_R_PACKAGE_PRESCAN: bool = true;

#[cfg(target_os = "macos")]
use libr::SEXP;

pub(crate) struct Bridge(crate::r_bridge::Bridge);

pub(crate) enum ResolutionFailureKind {
    Host,
    Interrupted,
}

impl ResolutionFailureKind {
    fn as_str(&self) -> &'static str {
        match self {
            Self::Host => "host",
            Self::Interrupted => "interrupted",
        }
    }
}

pub(crate) enum ResolutionOutcome {
    Resolved {
        library: String,
    },
    Failed {
        failure: ResolutionFailureKind,
        message: String,
    },
}

pub(crate) enum PreflightOutcome {
    Continue,
    Failed { message: String },
}

#[derive(serde::Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum PreparationOutcome {
    Prepared { library: String },
    Failed { message: String },
}

impl Bridge {
    pub(crate) fn initialize() -> Result<Self, String> {
        crate::r_bridge::Bridge::initialize(R_ENVIRONMENT_BRIDGE_SOURCE, "R environment").map(Self)
    }

    pub(crate) fn prepare(&self, library: &std::path::Path) -> Result<PreparationOutcome, String> {
        let library = library
            .to_str()
            .ok_or_else(|| "resolved R library path is not UTF-8".to_string())?;
        let response = self
            .0
            .call1_string(c"prepare", library)?
            .ok_or_else(|| "R environment bridge returned no response".to_string())?;
        serde_json::from_str(&response)
            .map_err(|error| format!("invalid R environment preparation response: {error}"))
    }

    pub(crate) fn preflight(&self, source: &str) -> Result<PreflightOutcome, String> {
        if !ENABLE_R_PACKAGE_PRESCAN {
            return Ok(PreflightOutcome::Continue);
        }
        let message = self
            .0
            .call1_string(c"preflight", source)?
            .ok_or_else(|| "R environment preflight returned no response".to_string())?;
        if message.is_empty() {
            Ok(PreflightOutcome::Continue)
        } else {
            Ok(PreflightOutcome::Failed { message })
        }
    }
}

#[cfg(target_os = "macos")]
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_resolve_r(packages: SEXP) -> harp::Result<SEXP> {
    let packages = Vec::<String>::try_from(harp::object::RObject::view(packages))?;
    let outcome = crate::worker::resolve_r(packages).map_err(|error| harp::anyhow!("{error}"))?;
    let response = match outcome {
        ResolutionOutcome::Resolved { library } => vec!["resolved".to_string(), library],
        ResolutionOutcome::Failed { failure, message } => {
            vec!["failed".to_string(), failure.as_str().to_string(), message]
        }
    };
    Ok(harp::object::RObject::from(response).sexp)
}

#[cfg(target_os = "macos")]
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_r_activated(library: SEXP) -> harp::Result<SEXP> {
    let library = String::try_from(harp::object::RObject::view(library))?;
    crate::worker::publish_r_activation(library).map_err(|error| harp::anyhow!("{error}"))?;
    unsafe { Ok(libr::R_NilValue) }
}

#[cfg(target_os = "macos")]
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_r_activation_failed(
    library: SEXP,
    message: SEXP,
) -> harp::Result<SEXP> {
    let library = String::try_from(harp::object::RObject::view(library))?;
    let message = String::try_from(harp::object::RObject::view(message))?;
    crate::worker::publish_r_activation_failure(library, message)
        .map_err(|error| harp::anyhow!("{error}"))?;
    unsafe { Ok(libr::R_NilValue) }
}
