//! Owned Linux containers. Image setup is separate from generation lifetime.
use crate::settings::{Compute, Pull, Target};
use crate::target_launch::{self, Bootstrap, Protocol};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::io::Read;
use std::path::PathBuf;
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

mod owner;
mod process;
const LIMIT: usize = 1024 * 1024;
const COMMAND_TIMEOUT: Duration = Duration::from_secs(10);
const PROTOCOL: Protocol = Protocol("Docker");
const LABEL: &str = "org.mcp-console.owner";

#[derive(Clone, Deserialize, Serialize)]
pub(crate) struct Session {
    target: Target,
    roots: Vec<PathBuf>,
    endpoint: Endpoint,
    image: String,
    digest: Option<String>,
    #[serde(skip)]
    blocked: Arc<Mutex<Option<String>>>,
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
            false,
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

impl Session {
    pub fn setup(
        target: Target,
        roots: Vec<PathBuf>,
        policy: &crate::settings::SandboxSettings,
        no_sandbox: bool,
        started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
        let cancel = process::Cancel::new()?;
        started(crate::resolver::ResolverStopHandle::new(cancel.clone()))?;
        let endpoint = Endpoint::capture(&cancel)?;
        let Compute::Docker(docker) = &target.compute else {
            unreachable!("Docker compute selected")
        };
        let inspect = |reference: &str| -> Result<Value, String> {
            let mut command = endpoint.command();
            command.args(["image", "inspect", "--", reference]);
            let bytes = process::run(
                command,
                &cancel,
                Some(Instant::now() + COMMAND_TIMEOUT),
                false,
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
                process::run(command, &cancel, None, true, None).map(|_| ())
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
            let path = std::env::temp_dir().join(format!("mcp-console-image-{}", token()?));
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
                process::run(command, &cancel, None, true, None)?;
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
        let session = Self {
            target,
            roots,
            endpoint,
            image: id,
            digest,
            blocked: Arc::default(),
        };
        let (command, request, _) = session.launch(policy, no_sandbox, true)?;
        let output = process::run(
            command,
            &cancel,
            Some(Instant::now() + Duration::from_secs(40)),
            false,
            Some(request),
        )?;
        let retirement = target_launch::Retirement::default();
        let mut output =
            target_launch::Output::new(std::io::Cursor::new(output), PROTOCOL, retirement.clone());
        let mut unexpected = Vec::new();
        output
            .read_to_end(&mut unexpected)
            .map_err(|e| e.to_string())?;
        retirement.check()?;
        if !unexpected.is_empty() {
            return Err("unexpected Docker runtime probe output".into());
        }
        Ok(session)
    }

    pub fn metadata(&self) -> Value {
        let Compute::Docker(docker) = &self.target.compute else {
            unreachable!()
        };
        json!({"transport": self.target.transport, "workspace": self.target.workspace,
            "compute": {"kind": "docker", "image": docker.image, "build": docker.build,
                "image_id": self.image, "repository_digest": self.digest}})
    }

    pub fn launch(
        &self,
        policy: &crate::settings::SandboxSettings,
        no_sandbox: bool,
        probe: bool,
    ) -> Result<(Command, Vec<u8>, String), String> {
        if let Some(error) = &*self
            .blocked
            .lock()
            .map_err(|_| "Docker session lock poisoned")?
        {
            return Err(error.clone());
        }
        let name = format!("mcp-console-{}", token()?);
        let request = owner::Request {
            session: self.clone(),
            name: name.clone(),
            probe,
            bootstrap: Bootstrap {
                version: target_launch::VERSION,
                build: env!("CARGO_PKG_VERSION").into(),
                workspace: self.target.workspace.clone(),
                policy: policy.clone(),
                writable_roots: self.roots.clone(),
                no_sandbox,
                environment: None,
            },
        };
        let mut command = Command::new(std::env::current_exe().map_err(|e| e.to_string())?);
        command.arg("docker-owner");
        Ok((command, target_launch::encode(&request)?, name))
    }

    pub fn check_retirement(
        &self,
        retirement: &target_launch::Retirement,
        name: &str,
    ) -> Result<(), String> {
        retirement.check().map_err(|error| {
            let error = format!(
                "Docker container '{name}': {error}; this session cannot start a replacement"
            );
            self.blocked
                .lock()
                .expect("Docker session lock")
                .get_or_insert(error.clone());
            error
        })
    }
}

fn token() -> Result<String, String> {
    let mut bytes = [0; 16];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut file| file.read_exact(&mut bytes))
        .map_err(|e| e.to_string())?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

pub(crate) fn run_owner() -> Result<(), String> {
    owner::run()
}

/// Resolve image defaults and workload selectors on the target, never controller paths.
pub(crate) fn configure_runtime(
    command: &mut Command,
    policy: &crate::settings::SandboxSettings,
) -> Result<(), String> {
    for name in ["R_HOME", "RETICULATE_PYTHON"] {
        let value = policy
            .get("environment")
            .and_then(|env| env.get(name))
            .and_then(Value::as_str)
            .map(str::to_string)
            .or_else(|| std::env::var(name).ok());
        match value {
            Some(value)
                if name != "RETICULATE_PYTHON" || (!value.is_empty() && value != "managed") =>
            {
                command.env(name, value);
            }
            None if name == "R_HOME" => {
                let output = Command::new("R")
                    .arg("RHOME")
                    .output()
                    .map_err(|error| format!("cannot discover container R: {error}"))?;
                if !output.status.success() {
                    return Err(format!(
                        "container R RHOME failed: {}",
                        String::from_utf8_lossy(&output.stderr)
                    ));
                }
                let home = String::from_utf8(output.stdout).map_err(|error| error.to_string())?;
                command.env(name, home.trim());
            }
            _ => {
                command.env_remove(name);
            }
        }
    }
    command
        .env("MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION", "0")
        .env("MCP_CONSOLE_EXECUTION_COMPUTE", "docker")
        .env("RETICULATE_USE_MANAGED_VENV", "no")
        .env_remove("MCP_CONSOLE_MANAGED_PYTHON")
        .env_remove("MCP_CONSOLE_PREINSTALLED");
    Ok(())
}

pub(crate) fn runtime_probe() -> Result<(), String> {
    let home =
        harp::command::r_home_setup().map_err(|e| format!("container R discovery failed: {e}"))?;
    let library = home.join("lib/libR.so");
    if !library.is_file() {
        return Err("container R requires a shared libR.so; install R with shared-library support in the image".into());
    }
    // Loadability is checked in this disposable target probe, without starting
    // R or the analysis worker. The image supplies this trusted native code.
    unsafe { libloading::Library::new(library) }
        .map_err(|error| format!("container R library cannot be loaded: {error}"))?;
    let selected = std::env::var_os("RETICULATE_PYTHON");
    if selected
        .as_ref()
        .is_some_and(|python| !PathBuf::from(python).is_file())
    {
        return Err("container RETICULATE_PYTHON must select an existing interpreter".into());
    }
    let python = selected.unwrap_or_else(|| "python3".into());
    let output = Command::new(python)
        .args(["-c", "import sys; assert sys.version_info.major == 3"])
        .output()
        .map_err(|error| format!("container Python probe failed: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "container Python probe failed with {}: {}",
            output.status,
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    Ok(())
}
