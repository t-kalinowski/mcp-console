use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Target {
    #[serde(default)]
    pub transport: Transport,
    #[serde(default)]
    pub compute: Compute,
    #[serde(default)]
    pub workspace: String,
    #[serde(default)]
    pub command: Option<Vec<String>>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum Transport {
    Local {},
    Ssh { host: String },
}

impl Default for Transport {
    fn default() -> Self {
        Self::Local {}
    }
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum Compute {
    Host {},
    Docker(Docker),
}

impl Default for Compute {
    fn default() -> Self {
        Self::Host {}
    }
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Docker {
    pub image: Option<String>,
    pub build: Option<Build>,
    pub pull: Option<Pull>,
    #[serde(default)]
    pub mounts: Vec<Mount>,
    pub user: Option<String>,
}

#[derive(Clone, Copy, Default, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Pull {
    Never,
    #[default]
    IfMissing,
    Always,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Build {
    pub context: PathBuf,
    pub dockerfile: PathBuf,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Mount {
    pub source: PathBuf,
    pub target: String,
    #[serde(default)]
    pub access: Access,
}

#[derive(Clone, Default, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Access {
    #[default]
    ReadOnly,
    ReadWrite,
}

impl Mount {
    pub fn argument(&self) -> String {
        // Docker parses --mount as CSV, including quotes inside field values.
        let quote = |value: String| format!("\"{}\"", value.replace('"', "\"\""));
        format!(
            "type=bind,{},{},readonly={}",
            quote(format!("source={}", self.source.display())),
            quote(format!("target={}", self.target)),
            matches!(self.access, Access::ReadOnly)
        )
    }
}

impl Target {
    pub fn is_local_host(&self) -> bool {
        matches!(
            (&self.transport, &self.compute),
            (Transport::Local {}, Compute::Host {})
        )
    }

    pub fn command(&self) -> &[String] {
        self.command.as_deref().expect("target command captured")
    }

    pub fn host(&self) -> &str {
        let Transport::Ssh { host } = &self.transport else {
            unreachable!("only SSH sessions select an SSH host")
        };
        host
    }

    pub(super) fn capture(&mut self) -> Result<(), String> {
        if self.is_local_host() {
            if !self.workspace.is_empty() || self.command.is_some() {
                return Err("local host targets use the launch directory and built-in command; target.workspace and target.command require SSH or Docker".into());
            }
            return Ok(());
        }
        if !self.workspace.starts_with('/') || self.workspace.contains('\0') {
            return Err("target.workspace must be an absolute remote directory path".into());
        }
        if let Transport::Ssh { host } = &self.transport {
            if host.is_empty() || host.contains('\0') {
                return Err("target.transport.host must be a nonempty SSH destination".into());
            }
            if !matches!(self.compute, Compute::Host {}) {
                return Err(
                    "SSH plus Docker is not supported; Docker requires local transport".into(),
                );
            }
        }
        if self.command.is_none() {
            self.command = Some(match self.compute {
                Compute::Host {} => vec!["uvx".into(), "mcp-console".into()],
                Compute::Docker(_) => vec!["mcp-console".into()],
            });
        }
        if self.command().is_empty()
            || self.command()[0].is_empty()
            || self.command().iter().any(|arg| arg.contains('\0'))
        {
            return Err("target.command must be a nonempty argv with a nonempty executable and no NUL bytes".into());
        }
        if let Compute::Docker(docker) = &mut self.compute {
            match (&docker.image, &docker.build) {
                (Some(_), Some(_)) => {
                    return Err("target.compute.image and build are mutually exclusive".into());
                }
                (None, None) => return Err("target.compute requires image or build".into()),
                (None, Some(_)) if docker.pull.is_some() => {
                    return Err("target.compute.pull requires image, not build".into());
                }
                _ => {}
            }
            for (name, value) in [("image", &docker.image), ("user", &docker.user)] {
                if value
                    .as_ref()
                    .is_some_and(|value| value.is_empty() || value.contains('\0'))
                {
                    return Err(format!(
                        "target.compute.{name} must be nonempty and contain no NUL bytes"
                    ));
                }
            }
            let launch = std::env::current_dir().map_err(|error| error.to_string())?;
            let capture_path = |path: &mut PathBuf| -> Result<(), String> {
                let value = path.to_str().ok_or("target paths must be UTF-8")?;
                if value.is_empty() || value.contains('\0') {
                    return Err("target paths must be nonempty and contain no NUL bytes".into());
                }
                if !path.is_absolute() {
                    *path = launch.join(&path);
                }
                Ok(())
            };
            for mount in &mut docker.mounts {
                capture_path(&mut mount.source)?;
                if !Path::new(&mount.target).is_absolute() || mount.target.contains('\0') {
                    return Err(
                        "target.compute.mounts.target must be an absolute container path".into(),
                    );
                }
            }
            if let Some(build) = &mut docker.build {
                capture_path(&mut build.context)?;
                capture_path(&mut build.dockerfile)?;
            }
        }
        Ok(())
    }
}
