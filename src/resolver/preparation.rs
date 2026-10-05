#![cfg_attr(not(any(unix, windows)), allow(dead_code))]
//! Private preparation traffic, separate from the relay stream. The owner keeps
//! trusted startup choices; each operation completes and retires its own resolver
//! groups before returning a result. Session manifests and activation stay local.

use crate::resolver::{ManagedPython, ManagedR, ResolverControlOutcome};
use crate::worker_protocol::PythonRequirementManifest;
use serde::{Deserialize, Serialize};
use std::io::{Read, Write};

#[cfg(any(unix, windows))]
mod client;
#[cfg(any(unix, windows))]
mod host;
#[cfg(not(any(unix, windows)))]
mod unsupported;
#[cfg(any(unix, windows))]
pub(crate) use client::Preparation;
#[cfg(not(any(unix, windows)))]
pub(crate) use unsupported::Preparation;

const VERSION: u32 = 6;
const LIMIT: usize = 1024 * 1024;
const SETUP_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(30);

#[derive(Default, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Selections {
    pub r_home: Option<String>,
    pub python: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub native_python: Option<String>,
}

impl Selections {
    pub fn from_policy(
        policy: &crate::settings::SandboxSettings,
        native_python: Option<&std::path::Path>,
    ) -> Result<Self, String> {
        // Extract usable selectors without validating native policy on the
        // controller. Other shapes remain in the captured policy for the host.
        let selection = |name| {
            policy
                .get("environment")?
                .get(name)?
                .as_str()
                .map(str::to_owned)
        };
        Ok(Self {
            r_home: selection("R_HOME"),
            python: selection("RETICULATE_PYTHON"),
            native_python: native_python
                .map(|path| {
                    path.to_str()
                        .map(str::to_owned)
                        .ok_or("remote python selection is not UTF-8".to_string())
                })
                .transpose()?,
        })
    }
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Discovery {
    pub managed: bool,
    pub selections: Selections,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub duckdb_extension_directory: Option<std::path::PathBuf>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub local_r_home_bytes: Option<Vec<u8>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub local_has_uv: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub native: Option<NativeDiscovery>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct NativeDiscovery {
    pub selection: crate::local_runtime::Selection,
    pub python: Option<ManagedPython>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) enum Operation {
    Bootstrap,
    ResolveRStandalone {
        requirements: Vec<String>,
    },
    R {
        requirements: Vec<String>,
    },
    Python {
        requirements: PythonRequirementManifest,
        r: Option<ManagedR>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        selected_python: Option<std::path::PathBuf>,
    },
    PythonVersion {
        constraints: Vec<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        r: Option<ManagedR>,
    },
    InspectPython {
        executable: std::path::PathBuf,
    },
    Uv {
        r: ManagedR,
    },
    Duckdb {
        r: ManagedR,
        extensions: Vec<String>,
    },
    DuckdbPython {
        python: ManagedPython,
        extensions: Vec<String>,
        extension_directory: std::path::PathBuf,
    },
}

#[derive(Clone, Copy, Default, Deserialize, Serialize)]
pub(crate) enum Mode {
    #[default]
    R,
    PythonOnly,
    Custom,
    Auto,
}

impl Mode {
    fn is_r(&self) -> bool {
        matches!(self, Self::R)
    }
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
enum Input {
    Open {
        version: u32,
        build: String,
        workspace: String,
        selections: Selections,
        #[serde(default, skip_serializing_if = "Mode::is_r")]
        mode: Mode,
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
    ResultChunk {
        id: u64,
        text: String,
    },
    Completed {
        id: u64,
        result: Option<Result<serde_json::Value, String>>,
        control: Option<ResolverControlOutcome>,
        confirmed: bool,
    },
    Controlled {
        id: u64,
        result: Result<bool, String>,
    },
    Closed,
}

impl Output {
    fn write(&self, writer: &mut impl Write, local: bool) -> Result<(), String> {
        let write_message = |writer: &mut _, message: &Self| {
            if local {
                write_jsonl(writer, message)
            } else {
                write(writer, message)
            }
        };
        let Self::Completed {
            id,
            result: Some(result),
            control,
            confirmed,
        } = self
        else {
            return write_message(writer, self);
        };
        let result = serde_json::to_string(result).map_err(|error| error.to_string())?;
        if result.len() <= LIMIT / 8 {
            return write_message(writer, self);
        }
        // JSON can expand each text byte to six bytes. Leave room for the
        // envelope; only the terminal receipt completes the assembled result.
        let mut tail = result.as_str();
        while !tail.is_empty() {
            let end = tail.floor_char_boundary((LIMIT / 8).min(tail.len()));
            write_message(
                writer,
                &Self::ResultChunk {
                    id: *id,
                    text: tail[..end].to_owned(),
                },
            )?;
            tail = &tail[end..];
        }
        write_message(
            writer,
            &Self::Completed {
                id: *id,
                result: None,
                control: *control,
                confirmed: *confirmed,
            },
        )
    }
}

fn encode(message: &impl Serialize) -> Result<Vec<u8>, String> {
    let bytes = serde_json::to_vec(message).map_err(|error| error.to_string())?;
    if bytes.len() > LIMIT {
        return Err("SSH preparation message exceeds 1 MiB".into());
    }
    Ok(bytes)
}

fn write(writer: &mut impl Write, message: &impl Serialize) -> Result<(), String> {
    let bytes = encode(message)?;
    writer
        .write_all(&(bytes.len() as u32).to_be_bytes())
        .and_then(|()| writer.write_all(&bytes))
        .and_then(|()| writer.flush())
        .map_err(|error| error.to_string())
}

fn read<T: serde::de::DeserializeOwned>(reader: &mut impl Read) -> Result<T, String> {
    let bytes = crate::target_launch::read_payload(reader, LIMIT, crate::ssh::PROTOCOL)
        .map_err(|error| error.to_string())?;
    serde_json::from_slice(&bytes)
        .map_err(|error| format!("invalid SSH preparation message: {error}"))
}

fn write_jsonl(writer: &mut impl Write, message: &impl Serialize) -> Result<(), String> {
    let bytes = encode(message)?;
    writer
        .write_all(&bytes)
        .and_then(|()| writer.write_all(b"\n"))
        .and_then(|()| writer.flush())
        .map_err(|error| error.to_string())
}

fn read_jsonl<T: serde::de::DeserializeOwned>(reader: &mut impl Read) -> Result<T, String> {
    let mut bytes = Vec::new();
    loop {
        let mut byte = [0];
        let count = reader.read(&mut byte).map_err(|error| error.to_string())?;
        if count == 0 {
            return Err("resolver input closed".into());
        }
        if byte[0] == b'\n' {
            break;
        }
        bytes.push(byte[0]);
        if bytes.len() > LIMIT {
            return Err("resolver JSON line exceeds 1 MiB".into());
        }
    }
    serde_json::from_slice(&bytes).map_err(|error| format!("invalid resolver JSON: {error}"))
}

pub(crate) fn run() -> Result<(), String> {
    #[cfg(unix)]
    return host::run(false);
    #[cfg(not(unix))]
    Err("SSH preparation requires macOS or Linux".into())
}

pub(crate) fn run_local() -> Result<(), String> {
    #[cfg(any(unix, windows))]
    return host::run(true);
    #[cfg(not(any(unix, windows)))]
    Err("host resolution requires macOS, Linux, or Windows".into())
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct WorkerEnvironment {
    pub discovery: Discovery,
    pub r: Option<ManagedR>,
    pub python: Option<ManagedPython>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub native: Option<crate::local_runtime::Selection>,
}

impl WorkerEnvironment {
    #[cfg(unix)]
    pub fn configure(&self, command: &mut std::process::Command) -> Result<(), String> {
        let selected = self
            .native
            .as_ref()
            .and_then(|runtime| runtime.python.as_ref());
        let home = self
            .native
            .as_ref()
            .and_then(|runtime| runtime.r_home.as_deref())
            .or_else(|| {
                self.discovery
                    .selections
                    .r_home
                    .as_deref()
                    .map(std::path::Path::new)
            });
        if home.is_none() && selected.is_none() {
            return Err("SSH worker bootstrap has no selected runtime".into());
        }
        if let Some(home) = home {
            command.env("R_HOME", home);
        }
        if let Some(r) = &self.r {
            r.configure_worker(command)?;
        }
        command
            .env_remove("MCP_CONSOLE_MANAGED_PYTHON")
            .env_remove("MCP_CONSOLE_PREINSTALLED");
        if let Some(python) = &self.python {
            if let Some(selected) = selected
                && (!selected.managed
                    || std::path::Path::new(&selected.selected.embedding.python) != python.python())
            {
                return Err("SSH Python does not match its managed environment".into());
            }
            if !python.python().is_file() {
                return Err("resolved remote Python interpreter no longer exists".into());
            }
            python.configure_worker(command);
            command.env_remove("RETICULATE_USE_MANAGED_VENV");
        } else {
            if selected.is_some_and(|selected| selected.managed) {
                return Err("SSH managed Python selection has no environment".into());
            }
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
        if let Some(runtime) = &self.native {
            runtime.configure(command)?;
        } else {
            // Compatibility with a lazily selected R-capable launch.
            crate::local_runtime::Selection {
                r_home: home.map(std::path::Path::to_path_buf),
                python: None,
            }
            .configure(command)?;
        }
        command.env(
            "MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION",
            if self.discovery.managed || selected.is_some_and(|selected| selected.managed) {
                "1"
            } else {
                "0"
            },
        );
        Ok(())
    }
}
