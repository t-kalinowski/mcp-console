use std::fs::{self, DirBuilder};
use std::os::unix::fs::DirBuilderExt as _;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub(super) struct PrivateDirectory {
    path: PathBuf,
    remove_on_drop: bool,
}

impl PrivateDirectory {
    pub(super) fn new(kind: &str) -> Result<Self, String> {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|error| format!("failed to read the system clock: {error}"))?
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "mcp-console-{kind}-{}-{unique}",
            std::process::id()
        ));
        DirBuilder::new()
            .mode(0o700)
            .create(&path)
            .map_err(|error| format!("failed to create private {kind} directory: {error}"))?;
        let path = path
            .canonicalize()
            .map_err(|error| format!("failed to resolve private {kind} directory: {error}"))?;
        Ok(Self {
            path,
            remove_on_drop: true,
        })
    }

    pub(super) fn path(&self) -> &Path {
        &self.path
    }

    pub(super) fn preserve(&mut self) {
        self.remove_on_drop = false;
    }

    pub(super) fn remove(&mut self) -> Result<(), String> {
        if let Err(error) = fs::remove_dir_all(&self.path)
            && error.kind() != std::io::ErrorKind::NotFound
        {
            self.remove_on_drop = false;
            return Err(format!(
                "failed to remove private directory {}: {error}",
                self.path.display()
            ));
        }
        self.remove_on_drop = false;
        Ok(())
    }

    pub(super) fn confirm_removed(&mut self) -> Result<(), String> {
        match fs::symlink_metadata(&self.path) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                self.remove_on_drop = false;
                Ok(())
            }
            Ok(_) => {
                self.remove_on_drop = false;
                Err(format!(
                    "private sandbox runner did not remove target directory {}",
                    self.path.display()
                ))
            }
            Err(error) => {
                self.remove_on_drop = false;
                Err(format!(
                    "failed to inspect private directory {}: {error}",
                    self.path.display()
                ))
            }
        }
    }
}

impl Drop for PrivateDirectory {
    fn drop(&mut self) {
        if self.remove_on_drop {
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}
