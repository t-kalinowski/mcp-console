//! Owned Linux containers. Image setup is separate from generation lifetime.
use crate::settings::{Compute, Pull, Target};
use crate::target_launch::{self, Protocol};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::path::PathBuf;
use std::process::Command;
use std::time::{Duration, Instant};

#[cfg(unix)]
mod owner;
use crate::target_launch::process;
const COMMAND_TIMEOUT: Duration = Duration::from_secs(10);
pub(crate) const PROTOCOL: Protocol = Protocol("Docker");
pub(crate) const PROFILE: crate::target_session::ComputeProfile =
    crate::target_session::ComputeProfile {
        protocol: PROTOCOL,
        resource: "container",
        owner_command: "docker-owner",
        probe_output: process::OutputMode::Capture,
        probe_retirement_grace: Duration::from_secs(8),
        retirement_grace: Duration::from_secs(6),
    };

#[cfg(unix)]
const LABEL: &str = "org.mcp-console.owner";

#[derive(Clone, Deserialize, Serialize)]
pub(crate) struct Captured {
    pub target: Target,
    endpoint: Endpoint,
    image: String,
    digest: Option<String>,
}

#[derive(Clone, Deserialize, Serialize)]
struct Endpoint {
    host: String,
    tls: Vec<String>,
    config: Option<PathBuf>,
}

impl Endpoint {
    fn command(&self) -> Command {
        let mut command = Command::new("docker");
        command
            .arg("--host")
            .arg(&self.host)
            .args(&self.tls)
            .env_remove("DOCKER_CONTEXT")
            .env_remove("DOCKER_HOST")
            .env_remove("DOCKER_TLS")
            .env_remove("DOCKER_TLS_VERIFY")
            .env_remove("DOCKER_CERT_PATH");
        if let Some(config) = &self.config {
            command.arg("--config").arg(config);
        }
        command
    }

    fn capture(cancel: &process::Cancel) -> Result<Self, String> {
        let mut command = Command::new("docker");
        command.args(["context", "inspect"]);
        let output = process::run(
            command,
            cancel,
            Some(Instant::now() + COMMAND_TIMEOUT),
            process::OutputMode::Capture,
            None,
        )?;
        let contexts: Vec<Value> = serde_json::from_slice(&output)
            .map_err(|e| format!("invalid Docker context response: {e}"))?;
        let context = contexts.first().ok_or("Docker did not select a context")?;
        let docker = &context["Endpoints"]["docker"];
        let host = docker["Host"]
            .as_str()
            .ok_or("Docker context has no endpoint")?
            .to_string();
        let mut tls = Vec::new();
        if context["Name"] == "default" {
            let enabled = std::env::var_os("DOCKER_TLS").is_some_and(|s| !s.is_empty());
            let verify = std::env::var_os("DOCKER_TLS_VERIFY").is_some_and(|s| !s.is_empty());
            if enabled || verify {
                tls.push(if verify { "--tlsverify" } else { "--tls" }.into());
                let certs = std::env::var_os("DOCKER_CERT_PATH")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| {
                        std::env::var_os("DOCKER_CONFIG")
                            .map(PathBuf::from)
                            .unwrap_or_else(|| {
                                PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
                                    .join(".docker")
                            })
                    });
                tls.extend(tls_arguments(certs));
            }
        } else if context["TLSMaterial"]["docker"]
            .as_array()
            .is_some_and(|a| !a.is_empty())
        {
            tls.push(
                if docker["SkipTLSVerify"] == true {
                    "--tls"
                } else {
                    "--tlsverify"
                }
                .into(),
            );
            let path = context["Storage"]["TLSPath"]
                .as_str()
                .ok_or("Docker context has no TLS path")?;
            tls.extend(tls_arguments(PathBuf::from(path).join("docker")));
        }
        Ok(Self {
            host,
            tls,
            config: std::env::var_os("DOCKER_CONFIG").map(PathBuf::from),
        })
    }
}

fn tls_arguments(path: PathBuf) -> Vec<String> {
    [
        ("--tlscacert", "ca.pem"),
        ("--tlscert", "cert.pem"),
        ("--tlskey", "key.pem"),
    ]
    .into_iter()
    .flat_map(|(flag, file)| [flag.into(), path.join(file).to_string_lossy().into_owned()])
    .collect()
}

impl Captured {
    pub fn capture(target: Target, cancel: &process::Cancel) -> Result<Self, String> {
        let endpoint = Endpoint::capture(cancel)?;
        let Compute::Docker(docker) = &target.compute else {
            unreachable!("Docker compute selected")
        };
        let inspect = |reference: &str| -> Result<Value, String> {
            let mut command = endpoint.command();
            command.args(["image", "inspect", "--", reference]);
            let bytes = process::run(
                command,
                cancel,
                Some(Instant::now() + COMMAND_TIMEOUT),
                process::OutputMode::Capture,
                None,
            )?;
            let mut images: Vec<Value> = serde_json::from_slice(&bytes)
                .map_err(|e| format!("invalid Docker image response: {e}"))?;
            if images.len() != 1 {
                return Err("Docker must resolve exactly one image".into());
            }
            Ok(images.remove(0))
        };
        let image = if let Some(reference) = &docker.image {
            let pull = || {
                let mut command = endpoint.command();
                command.args(["image", "pull", "--", reference]);
                process::run(
                    command,
                    cancel,
                    None,
                    process::OutputMode::Diagnostics,
                    None,
                )
                .map(|_| ())
            };
            match docker.pull.unwrap_or_default() {
                Pull::Always => {
                    pull()?;
                    inspect(reference)?
                }
                Pull::Never => inspect(reference)?,
                Pull::IfMissing => match inspect(reference) {
                    Ok(image) => image,
                    Err(error) if error.contains("No such image:") => {
                        pull()?;
                        inspect(reference)?
                    }
                    Err(error) => return Err(error),
                },
            }
        } else {
            let build = docker.build.as_ref().expect("validated build");
            if !build.context.is_dir() || !build.dockerfile.is_file() {
                return Err("Docker build.context must be an existing controller directory and build.dockerfile an existing controller file".into());
            }
            let path = std::env::temp_dir().join(format!(
                "mcp-console-image-{}",
                target_launch::owner::token()?
            ));
            std::fs::create_dir(&path).map_err(|e| e.to_string())?;
            let iid = path.join("id");
            let result = (|| {
                let mut command = endpoint.command();
                command
                    .arg("build")
                    .arg("--iidfile")
                    .arg(&iid)
                    .arg("--file")
                    .arg(&build.dockerfile)
                    .arg("--")
                    .arg(&build.context);
                process::run(
                    command,
                    cancel,
                    None,
                    process::OutputMode::Diagnostics,
                    None,
                )?;
                let id = std::fs::read_to_string(&iid).map_err(|e| e.to_string())?;
                inspect(id.trim())
            })();
            let _ = std::fs::remove_dir_all(path);
            result?
        };
        if image["Os"] != "linux" {
            return Err("Docker targets require a Linux image".into());
        }
        let id = image["Id"]
            .as_str()
            .ok_or("Docker image has no immutable ID")?
            .to_string();
        if !id.starts_with("sha256:") {
            return Err("Docker image ID must be a sha256 identity".into());
        }
        let digest = image["RepoDigests"]
            .as_array()
            .and_then(|values| values.first())
            .and_then(Value::as_str)
            .map(str::to_string);
        Ok(Self {
            target,
            endpoint,
            image: id,
            digest,
        })
    }

    pub fn metadata(&self) -> Value {
        let Compute::Docker(docker) = &self.target.compute else {
            unreachable!()
        };
        json!({"transport": self.target.transport, "workspace": self.target.workspace,
            "compute": {"kind": "docker", "image": docker.image, "build": docker.build,
                "image_id": self.image, "repository_digest": self.digest}})
    }
}

pub(crate) fn run_owner() -> Result<(), String> {
    #[cfg(unix)]
    return owner::run();
    #[cfg(not(unix))]
    Err("Windows currently supports local execution only".into())
}
