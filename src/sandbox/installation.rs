use sha2::{Digest as _, Sha256};
use std::os::unix::fs::PermissionsExt as _;
use std::path::PathBuf;

include!(concat!(env!("OUT_DIR"), "/sandbox_runner_installation.rs"));

pub(super) fn private_runner() -> Result<PathBuf, String> {
    let executable = std::env::current_exe()
        .and_then(|path| path.canonicalize())
        .map_err(|error| format!("failed to locate the private sandbox runner: {error}"))?;
    let directory = executable
        .parent()
        .and_then(|path| path.parent())
        .ok_or_else(|| "failed to locate the private sandbox runner installation".to_string())?
        .join("libexec");
    for (name, digest) in EXPECTED_ARTIFACTS {
        let path = directory.join(name);
        let unavailable = || format!("the private sandbox runner artifact {name} is unavailable");
        let metadata = path.metadata().map_err(|_| unavailable())?;
        if !metadata.is_file() || metadata.permissions().mode() & 0o111 == 0 {
            return Err(unavailable());
        }
        let bytes = std::fs::read(&path).map_err(|_| unavailable())?;
        if Sha256::digest(bytes).as_slice() != digest {
            return Err(format!(
                "the private sandbox runner artifact {name} does not match this installation"
            ));
        }
    }
    Ok(directory.join("mcp-console-sandbox"))
}
