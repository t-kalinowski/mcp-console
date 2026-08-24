use std::collections::BTreeSet;

use super::state::{Environment, PythonEnvironment, ensure_python_additions_available};

pub(crate) struct Requirements {
    pub(crate) duckdb: Vec<String>,
    pub(crate) python: Vec<String>,
    pub(crate) r: Vec<String>,
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
        let Requirements { duckdb, python, r } = requirements;
        ensure_python_additions_available(environment, &python)?;

        let duckdb_additions = duckdb.into_iter().collect::<BTreeSet<_>>();
        let duckdb_changed = !duckdb_additions.is_subset(&environment.duckdb_extensions);
        let duckdb_extensions = environment
            .duckdb_extensions
            .union(&duckdb_additions)
            .cloned()
            .collect();

        let python_additions = python.into_iter().collect::<BTreeSet<_>>();
        let python_candidate = merge_python_requirements(
            environment
                .python
                .as_ref()
                .and_then(PythonEnvironment::managed),
            python_additions.iter().cloned().collect(),
        );

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
