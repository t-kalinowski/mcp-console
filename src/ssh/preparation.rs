//! Private preparation traffic, separate from the relay stream. The owner keeps
//! trusted startup choices; each operation completes and retires its own resolver
//! groups before returning a result. Session manifests and activation stay local.

use crate::resolver::{ManagedPython, ManagedR, ResolverControlOutcome};
use crate::worker_protocol::PythonRequirementManifest;
use serde::{Deserialize, Serialize};
use std::io::{Read, Write};

#[cfg(unix)]
mod client;
#[cfg(unix)]
mod host;
#[cfg(not(unix))]
mod unsupported;
#[cfg(unix)]
pub(crate) use client::Preparation;
#[cfg(not(unix))]
pub(crate) use unsupported::Preparation;

const VERSION: u32 = 1;
const LIMIT: usize = 1024 * 1024;

#[derive(Default, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Selections {
    pub r_home: Option<String>,
    pub python: Option<String>,
}

impl Selections {
    pub fn from_policy(policy: &crate::settings::SandboxSettings) -> Result<Self, String> {
        let mut selections = Self::default();
        if let Some(environment) = policy.get("environment") {
            let environment = environment
                .as_object()
                .ok_or("sandbox.environment must be a mapping")?;
            for (name, selection) in [
                ("R_HOME", &mut selections.r_home),
                ("RETICULATE_PYTHON", &mut selections.python),
            ] {
                if let Some(value) = environment.get(name) {
                    *selection = Some(
                        value
                            .as_str()
                            .ok_or_else(|| format!("{name} must be a string"))?
                            .into(),
                    );
                }
            }
        }
        Ok(selections)
    }
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Discovery {
    pub managed: bool,
    pub selections: Selections,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) enum Operation {
    Bootstrap,
    R {
        requirements: Vec<String>,
    },
    Python {
        requirements: PythonRequirementManifest,
        r: Option<ManagedR>,
    },
    PythonVersion {
        constraints: Vec<String>,
        r: ManagedR,
    },
    Duckdb {
        r: ManagedR,
        extensions: Vec<String>,
    },
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
enum Input {
    Open {
        version: u32,
        build: String,
        workspace: String,
        selections: Selections,
    },
    Run {
        id: u64,
        operation: Operation,
    },
    Control {
        id: u64,
        control: ResolverControlOutcome,
    },
    Close,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
enum Output {
    Hello {
        version: u32,
        build: String,
    },
    Completed {
        id: u64,
        result: Result<serde_json::Value, String>,
        control: Option<ResolverControlOutcome>,
        confirmed: bool,
    },
    Controlled {
        id: u64,
        result: Result<bool, String>,
    },
    Closed,
}

fn write(writer: &mut impl Write, message: &impl Serialize) -> Result<(), String> {
    let bytes = serde_json::to_vec(message).map_err(|error| error.to_string())?;
    if bytes.len() > LIMIT {
        return Err("SSH preparation message exceeds 1 MiB".into());
    }
    writer
        .write_all(&(bytes.len() as u32).to_be_bytes())
        .and_then(|()| writer.write_all(&bytes))
        .and_then(|()| writer.flush())
        .map_err(|error| error.to_string())
}

fn read<T: serde::de::DeserializeOwned>(reader: &mut impl Read) -> Result<T, String> {
    let bytes = super::read_payload(reader, LIMIT).map_err(|error| error.to_string())?;
    serde_json::from_slice(&bytes)
        .map_err(|error| format!("invalid SSH preparation message: {error}"))
}

pub(crate) fn run() -> Result<(), String> {
    #[cfg(unix)]
    return host::run();
    #[cfg(not(unix))]
    Err("SSH preparation requires macOS or Linux".into())
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct WorkerEnvironment {
    pub discovery: Discovery,
    pub r: Option<ManagedR>,
    pub python: Option<ManagedPython>,
}

impl WorkerEnvironment {
    #[cfg(unix)]
    pub fn configure(&self, command: &mut std::process::Command) -> Result<(), String> {
        if let Some(home) = &self.discovery.selections.r_home {
            command.env("R_HOME", home);
        }
        if let Some(r) = &self.r {
            r.configure_worker(command)?;
        }
        command.env(
            "MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION",
            if self.discovery.managed { "1" } else { "0" },
        );
        command.env_remove("MCP_CONSOLE_MANAGED_PYTHON");
        command.env_remove("MCP_CONSOLE_PREINSTALLED");
        if let Some(python) = &self.python {
            if !python.python().is_file() {
                return Err("resolved remote Python interpreter no longer exists".into());
            }
            python.configure_worker(command);
            command.env_remove("RETICULATE_USE_MANAGED_VENV");
        } else {
            command.env("RETICULATE_USE_MANAGED_VENV", "no");
            match &self.discovery.selections.python {
                Some(python) if !python.is_empty() && python != "managed" => {
                    command.env("RETICULATE_PYTHON", python);
                }
                _ => {
                    command.env_remove("RETICULATE_PYTHON");
                }
            }
        }
        Ok(())
    }
}
