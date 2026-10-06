use serde::{Deserialize, Serialize};

pub(crate) const ENVIRONMENT: &str = "MCP_CONSOLE_STARTUP";

/// Consume process-handoff data before native or interpreter setup.
///
/// # Safety
/// The process must still be single-threaded, with no concurrent environment readers.
pub(crate) unsafe fn take_environment() -> Option<std::ffi::OsString> {
    let value = std::env::var_os(ENVIRONMENT);
    // SAFETY: the caller owns the single-threaded process bootstrap.
    unsafe { std::env::remove_var(ENVIRONMENT) };
    value
}

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

    /// # Safety
    /// Must run at worker entry, before native or interpreter threads start.
    pub unsafe fn from_environment() -> Result<Option<Self>, String> {
        // SAFETY: the caller owns the single-threaded worker bootstrap.
        let Some(value) = (unsafe { take_environment() }) else {
            return Ok(None);
        };
        let startup: Self = serde_json::from_str(value.to_str().ok_or("startup must be UTF-8")?)
            .map_err(|error| format!("invalid startup configuration: {error}"))?;
        startup.validate()?;
        Ok(Some(startup))
    }
}
