use sha2::{Digest as _, Sha256};
use std::os::unix::fs::PermissionsExt as _;
use std::path::PathBuf;

include!(concat!(env!("OUT_DIR"), "/sandbox_runner_installation.rs"));

pub(super) fn private_runner() -> Result<PathBuf, String> {
    #[cfg(debug_assertions)]
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/private-wheel-data/data/libexec/mcp-console-sandbox");
    #[cfg(not(debug_assertions))]
    let path = {
        let executable = std::env::current_exe()
            .and_then(|path| path.canonicalize())
            .map_err(|error| format!("failed to locate the private sandbox runner: {error}"))?;
        executable
            .parent()
            .and_then(|path| path.parent())
            .ok_or_else(|| "failed to locate the private sandbox runner installation".to_string())?
            .join("libexec/mcp-console-sandbox")
    };
    let unavailable = || "the private sandbox runner is unavailable".to_string();
    let metadata = path.metadata().map_err(|_| unavailable())?;
    if !metadata.is_file() || metadata.permissions().mode() & 0o111 == 0 {
        return Err(unavailable());
    }
    let bytes = std::fs::read(&path).map_err(|_| unavailable())?;
    if Sha256::digest(bytes).as_slice() != EXPECTED_RUNNER_SHA256 {
        return Err("the private sandbox runner does not match this installation".to_string());
    }
    Ok(path)
}
