use rmcp::schemars;
use std::collections::{BTreeMap, BTreeSet};

use super::state::{Environment, PythonEnvironment, ensure_managed_python_available};

#[derive(Clone, Copy, Default, PartialEq, Eq, serde::Deserialize, schemars::JsonSchema)]
#[serde(rename_all = "snake_case")]
#[schemars(inline)]
pub(crate) enum RequirementsAction {
    Get,
    #[default]
    Add,
    Set,
    Reset,
}

#[derive(Clone, Default)]
pub(crate) struct Requirements {
    pub(crate) call_id: Option<u64>,
    pub(crate) action: RequirementsAction,
    pub(crate) python_version: Vec<String>,
    pub(crate) exclude_newer: Option<Option<String>>,
    pub(crate) duckdb: Vec<String>,
    pub(crate) python: Vec<String>,
    pub(crate) r: Vec<String>,
}

impl Requirements {
    pub(in crate::worker_client) fn validate(&self) -> Result<(), String> {
        if self.action == RequirementsAction::Add
            && self.duckdb.is_empty()
            && self.r.is_empty()
            && self.python.is_empty()
            && self.python_version.is_empty()
            && self.exclude_newer.is_none()
        {
            return Err(
                "at least one of `requirements.r`, `requirements.python`, or `requirements.duckdb` is required"
                    .to_string(),
            );
        }
        if self.action == RequirementsAction::Add {
            for (name, length) in [
                ("duckdb", self.duckdb.len()),
                ("r", self.r.len()),
                ("python", self.python.len()),
            ] {
                if length > 64 {
                    let noun = if name == "duckdb" {
                        "extensions"
                    } else {
                        "requirements"
                    };
                    return Err(format!("`requirements.{name}` accepts at most 64 {noun}"));
                }
            }
        }
        validate_duckdb_extensions(&self.duckdb)?;
        validate_r_requirements(&self.r)?;
        crate::python_requirement::validate_all(&self.python)?;
        crate::python_requirement::validate_version_constraints(&self.python_version)
    }
}

fn validate_duckdb_extensions(extensions: &[String]) -> Result<(), String> {
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

pub(crate) fn validate_r_requirements(requirements: &[String]) -> Result<(), String> {
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

pub(in crate::worker_client) fn validate_policies(
    current: &super::inspection::Declaration,
    startup: &super::inspection::Declaration,
    r_policy: crate::settings::Resolution,
    python_policy: crate::settings::Resolution,
    has_r: bool,
    requested: &Requirements,
) -> Result<(), String> {
    if requested.action == RequirementsAction::Get {
        return Ok(());
    }
    let reset;
    let requested = if requested.action == RequirementsAction::Reset {
        reset = Requirements {
            action: RequirementsAction::Set,
            r: startup.r.clone(),
            python: startup.python.clone(),
            duckdb: startup.duckdb.clone(),
            python_version: startup.python_version.clone(),
            exclude_newer: Some(startup.exclude_newer.clone()),
            ..Default::default()
        };
        &reset
    } else {
        requested
    };
    let replacing = requested.action == RequirementsAction::Set;
    let changed = |before: &[String], after: &[String]| {
        let before = before.iter().collect::<BTreeSet<_>>();
        let after = after.iter().collect::<BTreeSet<_>>();
        if replacing {
            before != after
        } else {
            !after.is_subset(&before)
        }
    };
    if changed(&current.r, &requested.r) {
        if !r_policy.changes() {
            return Err(r_policy.reject("R"));
        }
        if !has_r {
            return Err("R requirements are unavailable in Python sessions without R".into());
        }
    }
    let python_changed = changed(&current.python, &requested.python)
        || changed(&current.python_version, &requested.python_version)
        || if replacing {
            requested.exclude_newer.clone().flatten() != current.exclude_newer
        } else {
            requested
                .exclude_newer
                .as_ref()
                .is_some_and(|cutoff| cutoff != &current.exclude_newer)
        };
    if python_changed && !python_policy.changes() {
        return Err(python_policy.reject("Python"));
    }
    if changed(&current.duckdb, &requested.duckdb) {
        let policy = if has_r { r_policy } else { python_policy };
        if !policy.changes() {
            return Err(policy.reject("DuckDB provider"));
        }
    }
    Ok(())
}

pub(in crate::worker_client) struct RequirementDelta {
    pub(super) restart_required: bool,
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
        validate_policies(
            &environment.declaration(),
            &environment.startup_declaration(),
            environment.r_policy(),
            environment.python_policy(),
            environment
                .local_runtime
                .as_ref()
                .is_none_or(|runtime| !runtime.python_only()),
            &requirements,
        )?;
        environment.validate_r_selection()?;
        if matches!(
            requirements.action,
            RequirementsAction::Set | RequirementsAction::Reset
        ) {
            return Self::replacement(environment, requirements);
        }
        let Requirements {
            duckdb,
            python,
            r,
            python_version,
            exclude_newer,
            ..
        } = requirements;
        let pending = matches!(environment.r_resolver, super::super::RResolver::Pending(_));
        let current = environment.declaration();
        let duckdb_additions = duckdb.into_iter().collect::<BTreeSet<_>>();
        let mut duckdb_extensions = current.duckdb.iter().cloned().collect::<BTreeSet<_>>();
        duckdb_extensions.extend(duckdb_additions);
        let duckdb_changed = duckdb_extensions != environment.duckdb_extensions;
        let python_additions = python.into_iter().collect::<BTreeSet<_>>();
        let mut candidate = current.python_manifest();
        candidate.packages.extend(python_additions.iter().cloned());
        let candidate = candidate.normalized();
        let python_changed = candidate != current.python_manifest();
        if python_changed {
            ensure_managed_python_available(environment)?;
        }
        let mut python_candidate = (python_changed
            || (pending
                && environment
                    .python
                    .as_ref()
                    .and_then(PythonEnvironment::managed)
                    .is_none()
                && environment.manages_python()))
        .then_some(candidate);

        let current_python = environment.declaration().python_manifest();
        let mut candidate = python_candidate
            .clone()
            .unwrap_or_else(|| current_python.clone());
        candidate.python_version.extend(python_version);
        if let Some(cutoff) = exclude_newer {
            if current_python.exclude_newer.is_some() && cutoff != current_python.exclude_newer {
                return Err("use requirements.action=\"set\" to replace exclude_newer".into());
            }
            candidate.exclude_newer = cutoff;
        }
        let candidate = candidate.normalized();
        let restart_required = candidate.python_version != current_python.python_version
            || candidate.exclude_newer != current_python.exclude_newer;
        if restart_required {
            ensure_managed_python_available(environment)?;
            python_candidate = Some(candidate);
        }
        let (r_requirements, r_changed) = merge_r_requirements(environment, r);

        Ok(Self {
            restart_required,
            duckdb_extensions,
            duckdb_changed,
            python_additions,
            python_candidate,
            r_requirements,
            r_changed,
        })
    }

    fn replacement(environment: &Environment, requirements: Requirements) -> Result<Self, String> {
        let current = environment.declaration();
        let candidate = if requirements.action == RequirementsAction::Reset {
            environment.startup_declaration()
        } else {
            super::inspection::Declaration {
                r: requirements.r,
                python: requirements.python,
                duckdb: requirements.duckdb,
                python_version: requirements.python_version,
                exclude_newer: requirements.exclude_newer.flatten(),
            }
            .normalized()
        };
        let changed = candidate != current;
        let pending = matches!(environment.r_resolver, super::super::RResolver::Pending(_));
        let python = candidate.python_manifest();
        let manages_python = environment.manages_python();
        if python != current.python_manifest() && !manages_python {
            ensure_managed_python_available(environment)?;
        }
        Ok(Self {
            restart_required: changed,
            duckdb_changed: changed && (candidate.duckdb != current.duckdb || pending),
            duckdb_extensions: candidate.duckdb.into_iter().collect(),
            python_additions: Default::default(),
            python_candidate: (changed
                && manages_python
                && (pending || python != current.python_manifest()))
            .then_some(python),
            r_changed: changed
                && (pending
                    || candidate.r != current.r
                    || (environment.custom_worker && environment.r.is_none())),
            r_requirements: candidate.r,
        })
    }

    pub(in crate::worker_client) fn is_empty(&self) -> bool {
        !self.duckdb_changed && self.python_candidate.is_none() && !self.r_changed
    }

    pub(super) fn is_live_duckdb_only(&self) -> bool {
        self.duckdb_changed
            && !self.restart_required
            && self.python_candidate.is_none()
            && !self.r_changed
    }

    pub(super) fn has_live_python_additions(&self) -> bool {
        self.python_candidate.is_some() && !self.restart_required && !self.r_changed
    }

    pub(super) fn validate_live_python_additions(
        &self,
        environment: &Environment,
    ) -> Result<(), String> {
        // This is declaration compatibility, not a resolved-version lock.
        // The complete candidate may resolve different dependency versions;
        // live activation does not promise arbitrary package hot-swapping.
        let retained = environment.declaration().python_manifest();
        let mut names = BTreeMap::new();
        for requirement in &retained.packages {
            names.insert(
                crate::python_requirement::distribution_name(requirement)?,
                requirement,
            );
        }
        for requirement in &self.python_additions {
            if retained.packages.contains(requirement) {
                continue;
            }
            let name = crate::python_requirement::distribution_name(requirement)?;
            if let Some(previous) = names.insert(name, requirement) {
                return Err(format!(
                    "live Python requirements can only add new distributions; `{requirement}` changes already-declared `{previous}`; use control: restart with requirements.action: set"
                ));
            }
        }
        Ok(())
    }
}

pub(super) fn merge_r_requirements(
    environment: &Environment,
    additions: Vec<String>,
) -> (Vec<String>, bool) {
    let additions = additions.into_iter().collect::<BTreeSet<_>>();
    let current = environment
        .declaration()
        .r
        .into_iter()
        .collect::<BTreeSet<_>>();
    let changed = !additions.is_subset(&current)
        || matches!(environment.r_resolver, super::super::RResolver::Pending(_))
        || (environment.custom_worker && environment.r.is_none());
    (current.union(&additions).cloned().collect(), changed)
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
    if !valid_module
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
