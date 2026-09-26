//! Console-owned files on the controller host.

use std::path::PathBuf;

pub(crate) fn home_console_directory() -> Result<Option<PathBuf>, String> {
    let Some(home) = std::env::var_os("HOME").filter(|value| !value.is_empty()) else {
        return Ok(None);
    };
    let home = PathBuf::from(home);
    if !home.is_absolute() {
        return Err("HOME must be an absolute path".into());
    }
    Ok(Some(home.join(".agents/console")))
}
