use std::collections::BTreeSet;

use super::state::{Environment, PythonEnvironment, ensure_python_additions_available};

pub(crate) struct Requirements {
    pub(crate) duckdb: Vec<String>,
    pub(crate) python: Vec<String>,
    pub(crate) r: Vec<String>,
}

impl Requirements {
    pub(in crate::worker_client) fn validate(&self) -> Result<(), String> {
        if self.duckdb.is_empty() && self.r.is_empty() && self.python.is_empty() {
            return Err(
                "at least one of `requirements.r`, `requirements.python`, or `requirements.duckdb` is required"
                    .to_string(),
            );
        }
        validate_duckdb_extensions(&self.duckdb)?;
        validate_r_requirements(&self.r)?;
        validate_python_requirements(&self.python)
    }
}

fn validate_duckdb_extensions(extensions: &[String]) -> Result<(), String> {
    if extensions.len() > 64 {
        return Err("`requirements.duckdb` accepts at most 64 extensions".to_string());
    }
    if extensions.iter().any(|extension| extension.len() > 64) {
        return Err("DuckDB extension names must be at most 64 ASCII characters".to_string());
    }
    if extensions.iter().any(|extension| {
        let mut bytes = extension.bytes();
        !bytes.next().is_some_and(|byte| byte.is_ascii_lowercase())
            || bytes
                .any(|byte| !(byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_'))
    }) {
        return Err(
            "DuckDB extension names must start with a lowercase ASCII letter and contain only lowercase ASCII letters, digits, and underscores"
                .to_string(),
        );
    }
    Ok(())
}

fn validate_r_requirements(requirements: &[String]) -> Result<(), String> {
    if requirements.len() > 64 {
        return Err("`requirements.r` accepts at most 64 requirements".to_string());
    }
    if requirements
        .iter()
        .any(|requirement| requirement.trim().is_empty())
    {
        return Err("R requirement strings must not be empty".to_string());
    }
    if requirements.iter().any(|requirement| {
        requirement
            .bytes()
            .any(|byte| matches!(byte, b'\0' | b'\r' | b'\n'))
    }) {
        return Err("R requirement strings must not contain NUL or line breaks".to_string());
    }
    Ok(())
}

fn validate_python_requirements(python: &[String]) -> Result<(), String> {
    if python.len() > 64 {
        return Err("`requirements.python` accepts at most 64 requirements".to_string());
    }
    crate::python_requirement::validate_all(python)
}

pub(in crate::worker_client) struct RequirementDelta {
    pub(super) duckdb_extensions: BTreeSet<String>,
    pub(super) duckdb_changed: bool,
    pub(super) python_additions: BTreeSet<String>,
    pub(super) python_candidate: Option<crate::worker_protocol::PythonRequirementManifest>,
    pub(super) r_requirements: Vec<String>,
    pub(super) r_changed: bool,
}

impl RequirementDelta {
    pub(in crate::worker_client) fn calculate(
        environment: &Environment,
        requirements: Requirements,
    ) -> Result<Self, String> {
        let Requirements {
            mut duckdb,
            python,
            r,
        } = requirements;
        ensure_python_additions_available(environment, &python)?;
        let pending = match &environment.r_resolver {
            super::super::RResolver::Pending(setup) => Some(setup),
            _ => None,
        };
        if pending.is_some() {
            duckdb.extend(
                super::super::DEFAULT_DUCKDB_EXTENSIONS
                    .iter()
                    .map(|name| (*name).to_string()),
            );
        }

        let duckdb_additions = duckdb.into_iter().collect::<BTreeSet<_>>();
        let duckdb_changed = !duckdb_additions.is_subset(&environment.duckdb_extensions);
        let duckdb_extensions = environment
            .duckdb_extensions
            .union(&duckdb_additions)
            .cloned()
            .collect();

        let python_additions = python.into_iter().collect::<BTreeSet<_>>();
        let mut python_candidate = merge_python_requirements(
            environment
                .python
                .as_ref()
                .and_then(PythonEnvironment::managed),
            python_additions.iter().cloned().collect(),
        );
        if python_candidate.is_none()
            && pending.is_some_and(|setup| {
                PythonEnvironment::uses_managed(setup.configured_python.as_deref())
            })
        {
            python_candidate = Some(crate::worker_protocol::default_python_requirement_manifest());
        }

        let (r_requirements, r_changed) = merge_r_requirements(environment, r);

        Ok(Self {
            duckdb_extensions,
            duckdb_changed,
            python_additions,
            python_candidate,
            r_requirements,
            r_changed,
        })
    }

    pub(in crate::worker_client) fn is_empty(&self) -> bool {
        !self.duckdb_changed && self.python_candidate.is_none() && !self.r_changed
    }
}

pub(super) fn merge_r_requirements(
    environment: &Environment,
    additions: Vec<String>,
) -> (Vec<String>, bool) {
    let mut additions = additions.into_iter().collect::<BTreeSet<_>>();
    if matches!(environment.r_resolver, super::super::RResolver::Pending(_)) {
        additions.extend(
            super::super::DEFAULT_R_REQUIREMENTS
                .iter()
                .map(|requirement| (*requirement).to_string()),
        );
    }
    if environment.custom_worker {
        additions.extend(
            super::super::CUSTOM_DUCKDB_R_REQUIREMENTS
                .iter()
                .map(|requirement| (*requirement).to_string()),
        );
    }
    let current = environment
        .r
        .as_ref()
        .map(|managed| managed.requirements().iter().cloned().collect())
        .unwrap_or_default();
    let changed = !additions.is_subset(&current);
    (current.union(&additions).cloned().collect(), changed)
}

fn merge_python_requirements(
    current: Option<&crate::resolver::ManagedPython>,
    additions: Vec<String>,
) -> Option<crate::worker_protocol::PythonRequirementManifest> {
    let retained = current
        .map(|managed| managed.requirements().packages.iter().cloned().collect())
        .unwrap_or_default();
    let mut candidate = current
        .map(|managed| managed.requirements().clone())
        .unwrap_or_else(crate::worker_protocol::default_python_requirement_manifest);
    let additions = additions.into_iter().collect::<BTreeSet<_>>();
    if additions.is_subset(&retained) {
        return None;
    }
    candidate.packages.extend(additions);
    Some(candidate.normalized())
}

pub(super) fn select_python_activation(
    current: Option<&crate::resolver::ManagedPython>,
    requirements: crate::worker_protocol::PythonRequirementManifest,
    candidate: Option<crate::resolver::ManagedPython>,
) -> Result<crate::resolver::ManagedPython, String> {
    let requirements = requirements.normalized();
    if let Some(candidate) = candidate {
        return (candidate.requirements() == &requirements)
            .then_some(candidate)
            .ok_or_else(|| {
                "worker activation does not match a resolved Python environment".to_string()
            });
    }
    current
        .cloned()
        .filter(|current| current.requirements() == &requirements)
        .ok_or_else(|| "worker activation does not match a resolved Python environment".to_string())
}

pub(super) fn validate_python_import_resolution(
    resolution: &crate::worker_protocol::PythonImportResolution,
    requirements: &crate::worker_protocol::PythonRequirementManifest,
) -> Result<(), String> {
    let module = resolution.module.as_bytes();
    let distribution = resolution.distribution.as_bytes();
    let valid_module = module
        .first()
        .is_some_and(|byte| byte.is_ascii_alphabetic() || *byte == b'_')
        && module
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || *byte == b'_');
    let valid_distribution = distribution.first().is_some_and(u8::is_ascii_alphanumeric)
        && distribution.last().is_some_and(u8::is_ascii_alphanumeric)
        && distribution
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(*byte, b'-' | b'_' | b'.'));
    if resolution.module == resolution.distribution
        || !valid_module
        || !valid_distribution
        || !requirements.packages.contains(&resolution.distribution)
    {
        return Err("invalid automatic Python import resolution metadata".to_string());
    }
    Ok(())
}

pub(super) fn select_r_activation(
    library: &str,
    candidates: &mut Vec<crate::resolver::ManagedR>,
) -> Result<crate::resolver::ManagedR, String> {
    let candidate = candidates
        .iter()
        .rposition(|candidate| candidate.library().to_str() == Some(library))
        .map(|index| candidates.remove(index));
    candidates.clear();
    candidate.ok_or_else(|| "worker activation does not match a resolved R environment".to_string())
}

pub(super) fn push_duckdb_r_target(
    targets: &mut Vec<crate::resolver::ManagedR>,
    candidate: crate::resolver::ManagedR,
) {
    if targets
        .iter()
        .all(|target| target.library() != candidate.library())
    {
        targets.push(candidate);
    }
}
