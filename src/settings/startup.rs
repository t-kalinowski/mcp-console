use serde::{Deserialize, Serialize};

pub(crate) const ENVIRONMENT: &str = "MCP_CONSOLE_STARTUP";

#[derive(Clone, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum Language {
    R,
    Python,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Startup {
    pub language: Language,
    pub code: String,
}

impl Startup {
    pub fn validate(&self) -> Result<(), String> {
        if self.code.trim().is_empty() || self.code.contains('\0') {
            return Err("startup.code must be nonempty source without NUL".into());
        }
        Ok(())
    }

    pub fn configure(&self, command: &mut std::process::Command) -> Result<(), String> {
        command.env(
            ENVIRONMENT,
            serde_json::to_string(self).map_err(|error| error.to_string())?,
        );
        Ok(())
    }

    pub fn from_environment() -> Result<Option<Self>, String> {
        let Some(value) = std::env::var_os(ENVIRONMENT) else {
            return Ok(None);
        };
        let startup: Self = serde_json::from_str(value.to_str().ok_or("startup must be UTF-8")?)
            .map_err(|error| format!("invalid startup configuration: {error}"))?;
        startup.validate()?;
        Ok(Some(startup))
    }
}
