//! Console-owned files on the controller host.

use std::path::PathBuf;

pub(crate) fn home_console_directory() -> Result<Option<PathBuf>, String> {
    if let Some(directory) = std::env::var_os("MCP_CONSOLE_HOME") {
        let directory = PathBuf::from(directory);
        if !directory.is_absolute() {
            return Err("MCP_CONSOLE_HOME must be an absolute path".into());
        }
        return Ok(Some(directory));
    }
    let Some(home) = std::env::var_os("HOME").filter(|value| !value.is_empty()) else {
        return Ok(None);
    };
    let home = PathBuf::from(home);
    if !home.is_absolute() {
        return Err("HOME must be an absolute path".into());
    }
    Ok(Some(home.join(".agents/console")))
}
