use serde::{Deserialize, Serialize};

pub(crate) const ENVIRONMENT: &str = "MCP_CONSOLE_SQL_SETTINGS";

#[derive(Clone, Copy, Default, PartialEq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Provider {
    #[default]
    Auto,
    R,
    Python,
}

#[derive(Clone, Default, PartialEq, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct Options {
    #[serde(skip_serializing_if = "Option::is_none")]
    threads: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    memory_limit: Option<String>,
}

#[derive(Clone, PartialEq, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct Sql {
    pub provider: Provider,
    database: String,
    read_only: bool,
    options: Options,
}

impl Default for Sql {
    fn default() -> Self {
        Self {
            provider: Provider::Auto,
            database: ":memory:".into(),
            read_only: false,
            options: Options::default(),
        }
    }
}

impl Sql {
    pub fn validate(&self) -> Result<(), String> {
        if self.database.trim().is_empty() {
            return Err("sql.database must not be empty".into());
        }
        if self.read_only && self.database == ":memory:" {
            return Err("sql.read_only requires a file-backed database".into());
        }
        if self
            .options
            .threads
            .is_some_and(|value| value == 0 || value > i32::MAX as u32)
        {
            return Err("sql.options.threads must be between 1 and 2147483647".into());
        }
        if self
            .options
            .memory_limit
            .as_ref()
            .is_some_and(|value| value.trim().is_empty())
        {
            return Err("sql.options.memory_limit must not be empty".into());
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

    pub fn from_environment() -> Result<Self, String> {
        let mut settings: Self = match std::env::var(ENVIRONMENT) {
            Ok(value) => serde_json::from_str(&value)
                .map_err(|error| format!("invalid SQL settings: {error}"))?,
            Err(std::env::VarError::NotPresent) => Self::default(),
            Err(error) => return Err(format!("cannot read SQL settings: {error}")),
        };
        settings.validate()?;
        // Capture on the execution host before interpreter hooks can change cwd.
        // The database need not exist yet, so do not canonicalize it.
        if settings.database != ":memory:" && std::path::Path::new(&settings.database).is_relative()
        {
            settings.database = std::path::absolute(&settings.database)
                .map_err(|error| format!("cannot resolve sql.database: {error}"))?
                .into_os_string()
                .into_string()
                .map_err(|_| "sql.database workspace path must be UTF-8")?;
        }
        Ok(settings)
    }
}
