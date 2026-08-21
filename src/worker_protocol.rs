use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

#[cfg(target_os = "macos")]
use crate::cell::Language;

pub(crate) const DEFAULT_PYTHON_PACKAGES: &[&str] = &["numpy", "pandas"];

#[cfg(target_os = "macos")]
pub(crate) fn deserialize_payload_free<'de, D>(deserializer: D) -> Result<(), D::Error>
where
    D: serde::Deserializer<'de>,
{
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct PayloadFree {}

    PayloadFree::deserialize(deserializer).map(drop)
}

#[cfg(target_os = "macos")]
#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum ServerMessage {
    Evaluate {
        language: Language,
        source: String,
    },
    PrepareR {
        library: String,
    },
    PreparePython {
        packages: Vec<String>,
    },
    PythonResolved {
        python: String,
    },
    PythonResolutionFailed {
        message: String,
    },
    PythonVersionResolved {
        version: String,
    },
    PythonVersionResolutionFailed {
        message: String,
    },
    #[serde(deserialize_with = "deserialize_payload_free")]
    Shutdown,
}

#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PythonRequirementManifest {
    pub(crate) packages: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub(crate) python_version: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) exclude_newer: Option<String>,
}

impl PythonRequirementManifest {
    pub(crate) fn normalized(mut self) -> Self {
        let mut packages = self.packages.into_iter().collect::<BTreeSet<_>>();
        self.packages = DEFAULT_PYTHON_PACKAGES
            .iter()
            .filter(|package| packages.remove(**package))
            .map(|package| (*package).to_string())
            .collect();
        self.packages.extend(packages);
        self.python_version = self
            .python_version
            .into_iter()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        self
    }
}

pub(crate) fn default_python_requirement_manifest() -> PythonRequirementManifest {
    PythonRequirementManifest {
        packages: DEFAULT_PYTHON_PACKAGES
            .iter()
            .map(|package| (*package).to_string())
            .collect(),
        ..Default::default()
    }
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PythonResolveRequest {
    pub(crate) requirements: PythonRequirementManifest,
    pub(crate) retained_requirements: PythonRequirementManifest,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PythonVersionResolveRequest {
    pub(crate) constraints: Vec<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ConsoleChannel {
    Output,
    Diagnostic,
}

#[cfg(target_os = "macos")]
#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum WorkerMessage {
    #[serde(deserialize_with = "deserialize_payload_free")]
    Ready,
    ConsoleOutput {
        data: String,
    },
    ConsoleDiagnostic {
        data: String,
    },
    Image {
        data: String,
        mime_type: String,
    },
    InputRequested {
        prompt: String,
    },
    #[serde(deserialize_with = "deserialize_payload_free")]
    InputReceived,
    #[serde(deserialize_with = "deserialize_payload_free")]
    InputCancelled,
    RPrepared {
        library: String,
    },
    RPreparationFailed {
        message: String,
    },
    ResolvePython {
        request: PythonResolveRequest,
    },
    ResolvePythonVersion {
        request: PythonVersionResolveRequest,
    },
    PythonActivated {
        requirements: PythonRequirementManifest,
    },
    #[serde(deserialize_with = "deserialize_payload_free")]
    PythonPrepared,
    PythonPreparationFailed {
        message: String,
    },
    #[serde(deserialize_with = "deserialize_payload_free")]
    Completed,
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::{ServerMessage, WorkerMessage};

    fn assert_encoding(message: &impl serde::Serialize, expected: &str) {
        assert_eq!(serde_json::to_string(message).unwrap(), expected);
    }

    fn accepted_unknown_fields<T: serde::de::DeserializeOwned>(
        messages: &[&'static str],
    ) -> Vec<&'static str> {
        messages
            .iter()
            .copied()
            .filter(|message| serde_json::from_str::<T>(message).is_ok())
            .collect()
    }

    #[test]
    fn payload_free_server_messages_reject_unknown_fields() {
        let accepted =
            accepted_unknown_fields::<ServerMessage>(&[r#"{"kind":"shutdown","obsolete":true}"#]);
        assert!(accepted.is_empty(), "accepted unknown fields: {accepted:?}");
    }

    #[test]
    fn payload_free_server_messages_retain_their_encoding() {
        assert_encoding(&ServerMessage::Shutdown, r#"{"kind":"shutdown"}"#);
    }

    #[test]
    fn payload_free_worker_messages_reject_unknown_fields() {
        let accepted = accepted_unknown_fields::<WorkerMessage>(&[
            r#"{"kind":"ready","obsolete":true}"#,
            r#"{"kind":"input_received","obsolete":true}"#,
            r#"{"kind":"input_cancelled","obsolete":true}"#,
            r#"{"kind":"python_prepared","obsolete":true}"#,
            r#"{"kind":"completed","obsolete":true}"#,
            r#"{"kind":"python_prepared","python_checkpoint":{"packages":[]}}"#,
            r#"{"kind":"completed","python_checkpoint":{"packages":[]}}"#,
        ]);
        assert!(accepted.is_empty(), "accepted unknown fields: {accepted:?}");
    }

    #[test]
    fn payload_free_worker_messages_retain_their_encoding() {
        for (message, expected) in [
            (WorkerMessage::Ready, r#"{"kind":"ready"}"#),
            (WorkerMessage::InputReceived, r#"{"kind":"input_received"}"#),
            (
                WorkerMessage::InputCancelled,
                r#"{"kind":"input_cancelled"}"#,
            ),
            (
                WorkerMessage::PythonPrepared,
                r#"{"kind":"python_prepared"}"#,
            ),
            (WorkerMessage::Completed, r#"{"kind":"completed"}"#),
        ] {
            assert_encoding(&message, expected);
        }
    }
}
