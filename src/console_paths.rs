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
    let (home, name) = match std::env::var_os("HOME").filter(|value| !value.is_empty()) {
        Some(home) => (PathBuf::from(home), "HOME"),
        None => {
            #[cfg(windows)]
            {
                // Rust's native discovery uses USERPROFILE, then the Windows
                // user-profile API. Explicit HOME always takes precedence.
                let Some(home) = std::env::home_dir() else {
                    return Ok(None);
                };
                (home, "Windows user home")
            }
            #[cfg(not(windows))]
            return Ok(None);
        }
    };
    if !home.is_absolute() {
        return Err(format!("{name} must be an absolute path"));
    }
    Ok(Some(home.join(".agents").join("console")))
}
