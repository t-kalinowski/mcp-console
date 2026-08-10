use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

#[cfg(target_os = "macos")]
use crate::cell::Language;

#[cfg(target_os = "macos")]
#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum ServerMessage {
    Evaluate { language: Language, source: String },
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
        self.packages = packages
            .remove("numpy")
            .then(|| "numpy".to_string())
            .into_iter()
            .chain(packages)
            .collect();
        self.python_version = self
            .python_version
            .into_iter()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        self
    }
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PythonResolveRequest {
    pub(crate) requirements: PythonRequirementManifest,
    pub(crate) environment: BTreeMap<String, String>,
}

#[cfg(target_os = "macos")]
#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum WorkerMessage {
    Ready,
    Output {
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
    Completed {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        python_checkpoint: Option<PythonRequirementManifest>,
    },
}
