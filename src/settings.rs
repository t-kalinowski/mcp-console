//! Trusted application settings, captured before starting a session or workload.

use std::collections::BTreeMap;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

mod yaml;

pub const ENVIRONMENT: &str = "MCP_CONSOLE_SANDBOX_SETTINGS";

/// Normalized application inputs. Native policy remains in the sandbox layer.
#[derive(Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SandboxSettings {
    pub writable_roots: Vec<PathBuf>,
    pub network: Network,
    pub proxy: Option<Proxy>,
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Project {
    sandbox: Mapping<Sandbox>,
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Sandbox {
    filesystem: Mapping<Filesystem>,
    network: StringEnum<Network>,
    proxy: Option<Mapping<Proxy>>,
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Filesystem {
    kind: StringEnum<Restricted>,
    entries: Vec<Mapping<WriteEntry>>,
}

#[derive(Default, Deserialize)]
#[serde(rename_all = "lowercase")]
enum Restricted {
    #[default]
    Restricted,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WriteEntry {
    path: Mapping<LiteralPath>,
    access: StringEnum<Write>,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum Write {
    Write,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LiteralPath {
    #[serde(rename = "type")]
    kind: StringEnum<PathKind>,
    path: PathBuf,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum PathKind {
    Path,
}

#[derive(Default, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Network {
    #[default]
    Restricted,
    Enabled,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Proxy {
    #[serde(deserialize_with = "enabled_proxy")]
    pub enabled: bool,
    #[serde(default = "yes")]
    pub enable_socks5: bool,
    #[serde(default)]
    pub mode: StringEnum<ProxyMode>,
    #[serde(default)]
    pub domains: Option<BTreeMap<String, StringEnum<DomainPermission>>>,
    #[serde(default)]
    pub allow_upstream_proxy: bool,
    #[serde(default)]
    pub allow_local_binding: bool,
}

#[derive(Default, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ProxyMode {
    #[default]
    Full,
    Limited,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum DomainPermission {
    None,
    Allow,
    Deny,
}

fn yes() -> bool {
    true
}

fn enabled_proxy<'de, D: serde::Deserializer<'de>>(deserializer: D) -> Result<bool, D::Error> {
    if bool::deserialize(deserializer)? {
        Ok(true)
    } else {
        Err(serde::de::Error::custom("proxy requires enabled: true"))
    }
}

/// Only string wire values are settings, not Serde's tagged enum objects.
#[derive(Default, Serialize)]
#[serde(transparent)]
pub struct StringEnum<T>(T);

impl<'de, T: Deserialize<'de>> Deserialize<'de> for StringEnum<T> {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        T::deserialize(serde::de::value::StringDeserializer::new(
            String::deserialize(deserializer)?,
        ))
        .map(Self)
    }
}

/// Serde's derived structs also accept positional sequences. YAML settings
/// require named mappings, including for structs whose fields all have defaults.
#[derive(Default)]
struct Mapping<T>(T);

impl<'de, T: Deserialize<'de>> Deserialize<'de> for Mapping<T> {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Visitor<T>(std::marker::PhantomData<T>);
        impl<'de, T: Deserialize<'de>> serde::de::Visitor<'de> for Visitor<T> {
            type Value = Mapping<T>;
            fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str("a mapping")
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                map: A,
            ) -> Result<Self::Value, A::Error> {
                T::deserialize(serde::de::value::MapAccessDeserializer::new(map)).map(Mapping)
            }
        }
        deserializer.deserialize_map(Visitor(std::marker::PhantomData))
    }
}

pub fn discover() -> Result<SandboxSettings, String> {
    let mut selected = None;
    for name in [".mcp-console/config.yaml", ".agents/mcp-console.yaml"] {
        // A dangling symlink or an unreadable existing file must reach read_to_string.
        match std::fs::symlink_metadata(name) {
            // No configuration file can exist below a non-directory component.
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
                ) =>
            {
                continue;
            }
            Err(error) => return Err(format!("cannot inspect '{name}': {error}")),
            Ok(_) => {}
        }
        if let Some(previous) = selected {
            return Err(format!(
                "ambiguous project configuration: '{previous}' and '{name}'"
            ));
        }
        selected = Some(name);
    }
    let Some(name) = selected else {
        return Ok(SandboxSettings::default());
    };
    let source =
        std::fs::read_to_string(name).map_err(|error| format!("cannot read '{name}': {error}"))?;
    let value = yaml::load(&source).map_err(|error| format!("{name}: {error}"))?;
    let project: Project =
        serde_path_to_error::deserialize(value).map_err(|error| format!("{name}: {error}"))?;
    let Mapping(Sandbox {
        filesystem,
        network: StringEnum(network),
        proxy,
    }) = project.sandbox;
    let Mapping(Filesystem {
        kind: StringEnum(Restricted::Restricted),
        entries,
    }) = filesystem;
    Ok(SandboxSettings {
        writable_roots: entries
            .into_iter()
            .map(|Mapping(entry)| {
                let WriteEntry {
                    path,
                    access: StringEnum(Write::Write),
                } = entry;
                let Mapping(LiteralPath {
                    kind: StringEnum(PathKind::Path),
                    path,
                }) = path;
                path
            })
            .collect(),
        network,
        proxy: proxy.map(|Mapping(proxy)| proxy),
    })
}

pub fn from_environment(name: &str) -> Result<SandboxSettings, String> {
    let value = std::env::var(name)
        .map_err(|error| format!("cannot read sandbox settings environment '{name}': {error}"))?;
    serde_json::from_str(&value)
        .map_err(|error| format!("invalid sandbox settings environment '{name}': {error}"))
}
