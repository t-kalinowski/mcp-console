//! Trusted application settings, captured before starting a session or workload.

use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use serde_json::Value;

mod yaml;

pub const ENVIRONMENT: &str = "MCP_CONSOLE_SANDBOX_SETTINGS";

/// Normalized application inputs. Native policy remains in the sandbox layer.
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SandboxSettings {
    pub writable_roots: Vec<PathBuf>,
    pub network: Value,
    pub proxy: Option<Proxy>,
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Project {
    sandbox: Mapping<Sandbox>,
}

#[derive(Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Sandbox {
    filesystem: Mapping<Filesystem>,
    network: Value,
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

// Native values remain JSON so the runner owns their types and validation.
#[derive(Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Proxy {
    #[serde(default)]
    pub enabled: Value,
    #[serde(default = "yes")]
    pub enable_socks5: Value,
    #[serde(default = "full")]
    pub mode: Value,
    #[serde(default)]
    pub domains: Value,
    #[serde(default = "no")]
    pub allow_upstream_proxy: Value,
    #[serde(default = "no")]
    pub allow_local_binding: Value,
}

impl Default for Sandbox {
    fn default() -> Self {
        Self {
            filesystem: Mapping::default(),
            network: "restricted".into(),
            proxy: None,
        }
    }
}

impl Default for SandboxSettings {
    fn default() -> Self {
        Self {
            writable_roots: Vec::new(),
            network: Sandbox::default().network,
            proxy: None,
        }
    }
}

fn yes() -> Value {
    true.into()
}

fn no() -> Value {
    false.into()
}

fn full() -> Value {
    "full".into()
}

/// Filesystem entry forms use string discriminants in the additive interface.
#[derive(Default)]
struct StringEnum<T>(T);

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

pub fn discover() -> Result<(Option<&'static str>, SandboxSettings), String> {
    let name = ".agents/console/config.yaml";
    // A dangling symlink or an unreadable existing file must reach read_to_string.
    match std::fs::symlink_metadata(name) {
        // No configuration file can exist below a non-directory component.
        Err(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
            ) =>
        {
            return Ok((None, SandboxSettings::default()));
        }
        Err(error) => return Err(format!("cannot inspect '{name}': {error}")),
        Ok(_) => {}
    }
    let source =
        std::fs::read_to_string(name).map_err(|error| format!("cannot read '{name}': {error}"))?;
    let value = yaml::load(&source).map_err(|error| format!("{name}: {error}"))?;
    let project: Project =
        serde_path_to_error::deserialize(value).map_err(|error| format!("{name}: {error}"))?;
    let Mapping(Sandbox {
        filesystem,
        network,
        proxy,
    }) = project.sandbox;
    let Mapping(Filesystem {
        kind: StringEnum(Restricted::Restricted),
        entries,
    }) = filesystem;
    Ok((
        Some(name),
        SandboxSettings {
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
        },
    ))
}

pub fn from_environment(name: &str) -> Result<SandboxSettings, String> {
    let value = std::env::var(name)
        .map_err(|error| format!("cannot read sandbox settings environment '{name}': {error}"))?;
    serde_json::from_str(&value)
        .map_err(|error| format!("invalid sandbox settings environment '{name}': {error}"))
}
