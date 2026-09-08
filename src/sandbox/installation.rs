use sha2::{Digest as _, Sha256};
use std::fs::File;
use std::io::{self, Read as _};
use std::os::unix::fs::PermissionsExt as _;
use std::path::PathBuf;

include!(concat!(env!("OUT_DIR"), "/sandbox_runner_installation.rs"));

pub(super) fn private_runner() -> Result<PathBuf, String> {
    let verify = || -> io::Result<PathBuf> {
        let executable = std::env::current_exe()?.canonicalize()?;
        let prefix = executable
            .parent()
            .and_then(|directory| directory.parent())
            .ok_or_else(|| io::Error::other("executable has no installation prefix"))?;
        for (relative, expected) in ARTIFACTS {
            let mut file = File::open(prefix.join(relative))?;
            let metadata = file.metadata()?;
            if !metadata.is_file()
                || (relative.starts_with("libexec/") && metadata.permissions().mode() & 0o111 == 0)
            {
                return Err(io::Error::other(
                    "private artifact is not a readable file or executable",
                ));
            }
            let mut digest = Sha256::new();
            let mut buffer = [0; 64 * 1024];
            loop {
                let count = match file.read(&mut buffer) {
                    Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                    result => result?,
                };
                if count == 0 {
                    break;
                }
                digest.update(&buffer[..count]);
            }
            if digest.finalize().as_slice() != expected {
                return Err(io::Error::other(
                    "private artifact does not match this installation",
                ));
            }
        }
        Ok(prefix.join("libexec/mcp-console-sandbox"))
    };
    verify().map_err(|error| format!("failed to verify the private sandbox runner: {error}"))
}
