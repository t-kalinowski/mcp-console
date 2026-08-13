use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

#[cfg(target_os = "macos")]
use crate::cell::Language;

pub(crate) const DEFAULT_PYTHON_PACKAGES: &[&str] = &["numpy", "pandas"];

#[cfg(target_os = "macos")]
#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum ServerMessage {
    Evaluate { language: Language, source: String },
    PreparePython { packages: Vec<String> },
    PythonResolved { python: String },
    PythonResolutionFailed { message: String },
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
    pub(crate) environment: BTreeMap<String, String>,
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
    InputReceived,
    ResolvePython {
        request: PythonResolveRequest,
    },
    PythonPrepared {
        python_checkpoint: PythonRequirementManifest,
    },
    PythonPreparationFailed {
        message: String,
    },
    Completed {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        python_checkpoint: Option<PythonRequirementManifest>,
    },
}
