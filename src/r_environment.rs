const R_ENVIRONMENT_BRIDGE_SOURCE: &str = include_str!("r_environment/bridge.R");

pub(crate) struct Bridge(crate::r_bridge::Bridge);

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
}
