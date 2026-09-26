//! Console-owned files on the controller host.

use std::path::PathBuf;

pub(crate) fn home_console_directory() -> Result<PathBuf, String> {
    let home = std::env::var_os("HOME")
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "HOME is not set".to_string())?;
    let home = PathBuf::from(home);
    if !home.is_absolute() {
        return Err("HOME must be an absolute path".into());
    }
    Ok(home.join(".agents/console"))
}
