use std::fs::{self, DirBuilder};
use std::io::{self, Write as _};
use std::os::unix::fs::{DirBuilderExt as _, PermissionsExt as _};
use std::path::PathBuf;

include!(concat!(env!("OUT_DIR"), "/sandbox_runner_installation.rs"));

const RUNNER: &[u8] = include_bytes!(concat!(
    env!("OUT_DIR"),
    "/sandbox-runner/mcp-console-sandbox"
));
const LICENSE: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/sandbox-runner/LICENSE"));
const NOTICE: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/sandbox-runner/NOTICE"));

pub(super) fn private_runner() -> Result<PathBuf, String> {
    let prepare = || -> io::Result<PathBuf> {
        let home = std::env::home_dir().ok_or_else(|| {
            io::Error::other("could not locate the home directory for the runner cache")
        })?;
        let directory = home
            .join("Library/Caches/mcp-console/sandbox")
            .join(BUNDLE_SHA256);
        DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&directory)?;
        for (name, bytes, mode) in [
            ("LICENSE", LICENSE, 0o400),
            ("NOTICE", NOTICE, 0o400),
            ("mcp-console-sandbox", RUNNER, 0o500),
        ] {
            let path = directory.join(name);
            if !path.try_exists()? {
                let mut file = tempfile::NamedTempFile::new_in(&directory)?;
                file.write_all(bytes)?;
                file.as_file()
                    .set_permissions(fs::Permissions::from_mode(mode))?;
                // Concurrent first launches may publish the same embedded bytes.
                if let Err(error) = file.persist_noclobber(&path)
                    && error.error.kind() != io::ErrorKind::AlreadyExists
                {
                    return Err(error.error);
                }
            }
            if fs::read(&path)? != bytes || path.metadata()?.permissions().mode() & mode != mode {
                return Err(io::Error::other(
                    "cached artifact does not match this installation",
                ));
            }
        }
        Ok(directory.join("mcp-console-sandbox"))
    };
    prepare().map_err(|error| format!("failed to prepare the private sandbox runner: {error}"))
}
