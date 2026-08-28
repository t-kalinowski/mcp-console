use std::cmp::Ordering;
use std::collections::BTreeSet;

use pep508_rs::pep440_rs::{Version, VersionSpecifier};
use serde::Deserialize;

pub(super) struct PythonVersions {
    raw_empty: bool,
    candidates: Vec<Candidate>,
}

struct Candidate {
    version: String,
    parsed_version: Version,
    major: u64,
    minor: u64,
    patch: u64,
    prerelease: bool,
    uv_python: bool,
    latest_patch: bool,
}

#[derive(Deserialize)]
struct UvPython {
    version: String,
    version_parts: VersionParts,
    path: Option<String>,
    symlink: Option<String>,
    url: Option<String>,
    variant: String,
    implementation: String,
}

#[derive(Deserialize)]
struct VersionParts {
    major: u64,
    minor: u64,
    patch: u64,
}

#[derive(Clone, Copy)]
enum ConstraintOperator {
    Equal,
    NotEqual,
    LessThan,
    LessThanEqual,
    GreaterThan,
    GreaterThanEqual,
}

struct Constraint<'a> {
    operator: ConstraintOperator,
    numeric_version: Option<Vec<u64>>,
    specifier: Option<VersionSpecifier>,
    version_string: &'a str,
}

impl PythonVersions {
    pub(super) fn parse(output: &[u8], prefer_managed: bool) -> Result<Self, String> {
        let rows = serde_json::from_slice::<Vec<UvPython>>(output)
            .map_err(|error| format!("uv returned invalid Python inventory JSON: {error}"))?;
        let raw_empty = rows.is_empty();
        let mut candidates = rows
            .into_iter()
            .filter_map(Candidate::from_uv)
            .collect::<Vec<_>>();
        if !candidates.is_empty() {
            rank(&mut candidates, prefer_managed)?;
        }
        Ok(Self {
            raw_empty,
            candidates,
        })
    }

    pub(super) fn raw_empty(&self) -> bool {
        self.raw_empty
    }

    pub(super) fn resolve(&self, constraints: &[String]) -> Result<String, String> {
        let constraints = constraints
            .iter()
            .flat_map(|constraint| constraint.split(','))
            .map(str::trim)
            .filter(|constraint| !constraint.is_empty())
            .collect::<Vec<_>>();

        let Some(default) = self.candidates.first() else {
            return Err("uv did not report a supported CPython interpreter".to_string());
        };
        if constraints.is_empty() {
            return Ok(default.version.clone());
        }
        if constraints.len() == 1
            && self
                .candidates
                .iter()
                .any(|candidate| candidate.version == constraints[0])
        {
            return Ok(constraints[0].to_string());
        }

        let requested = constraints.join(",");
        let constraints = constraints
            .iter()
            .map(|constraint| Constraint::parse(constraint))
            .collect::<Vec<_>>();
        if let Some(candidate) = self.candidates.iter().find(|candidate| {
            constraints
                .iter()
                .all(|constraint| constraint.matches(candidate))
        }) {
            return Ok(candidate.version.clone());
        }

        let available = self
            .candidates
            .iter()
            .map(|candidate| candidate.version.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        Err(format!(
            "Requested Python version constraints could not be satisfied.\n  constraints: \"{requested}\"\nHint: Call `py_require(python_version = <string>, action = \"set\")` to replace constraints.\nAvailable Python versions found: {available}\n"
        ))
    }
}

impl Candidate {
    fn from_uv(row: UvPython) -> Option<Self> {
        if row.symlink.is_some() || row.variant != "default" || row.implementation != "cpython" {
            return None;
        }
        let VersionParts {
            major,
            minor,
            patch,
        } = row.version_parts;
        let parsed_version = row.version.parse().ok()?;
        let prerelease = row.version != format!("{major}.{minor}.{patch}");
        let downloaded = row
            .path
            .as_deref()
            .is_some_and(|path| path.replace('\\', "/").contains("/uv/python/"));
        Some(Self {
            version: row.version,
            parsed_version,
            major,
            minor,
            patch,
            prerelease,
            uv_python: row.url.is_some() || downloaded,
            latest_patch: false,
        })
    }

    fn preference_score(&self, preferred_minor: i128) -> i128 {
        let minor = i128::from(self.minor);
        -(minor - preferred_minor).abs() * 2 - if minor > preferred_minor { 1 } else { 0 }
    }
}

impl<'a> Constraint<'a> {
    fn parse(value: &'a str) -> Self {
        let (operator, version_string, explicit_operator) =
            if let Some(version) = value.strip_prefix(">=") {
                (ConstraintOperator::GreaterThanEqual, version, true)
            } else if let Some(version) = value.strip_prefix("<=") {
                (ConstraintOperator::LessThanEqual, version, true)
            } else if let Some(version) = value.strip_prefix("==") {
                (ConstraintOperator::Equal, version, true)
            } else if let Some(version) = value.strip_prefix("!=") {
                (ConstraintOperator::NotEqual, version, true)
            } else if let Some(version) = value.strip_prefix('>') {
                (ConstraintOperator::GreaterThan, version, true)
            } else if let Some(version) = value.strip_prefix('<') {
                (ConstraintOperator::LessThan, version, true)
            } else {
                (ConstraintOperator::Equal, value, false)
            };
        let version_string = version_string.trim().trim_end_matches(".*");
        let numeric_version = parse_numeric_version(version_string);
        let specifier = (numeric_version.is_none() && explicit_operator)
            .then(|| value.parse::<VersionSpecifier>().ok())
            .flatten();
        Self {
            operator,
            numeric_version,
            specifier,
            version_string,
        }
    }

    fn matches(&self, candidate: &Candidate) -> bool {
        if let Some(specifier) = self.specifier.as_ref() {
            return specifier.contains(&candidate.parsed_version);
        }
        let Some(version) = self.numeric_version.as_ref() else {
            return candidate.version == self.version_string;
        };
        let mut candidate = vec![candidate.major, candidate.minor, candidate.patch];
        let mut version = version.clone();
        let specified_levels = version.len();
        if specified_levels < 3 {
            version.resize(3, 0);
            candidate[2] = 0;
        }
        if specified_levels < 2 {
            candidate[1] = 0;
        }
        let length = candidate.len().max(version.len());
        candidate.resize(length, 0);
        version.resize(length, 0);
        let ordering = candidate.cmp(&version);
        match self.operator {
            ConstraintOperator::Equal => ordering == Ordering::Equal,
            ConstraintOperator::NotEqual => ordering != Ordering::Equal,
            ConstraintOperator::LessThan => ordering == Ordering::Less,
            ConstraintOperator::LessThanEqual => ordering != Ordering::Greater,
            ConstraintOperator::GreaterThan => ordering == Ordering::Greater,
            ConstraintOperator::GreaterThanEqual => ordering != Ordering::Less,
        }
    }
}

fn parse_numeric_version(version: &str) -> Option<Vec<u64>> {
    let parts = version.split('.').collect::<Vec<_>>();
    if parts.iter().any(|part| part.is_empty()) {
        return None;
    }
    parts
        .into_iter()
        .map(str::parse::<u64>)
        .collect::<Result<Vec<_>, _>>()
        .ok()
}

fn rank(candidates: &mut [Candidate], prefer_managed: bool) -> Result<(), String> {
    candidates.sort_by(|left, right| initial_order(left, right, prefer_managed));
    let mut seen = BTreeSet::new();
    for candidate in candidates.iter_mut() {
        candidate.latest_patch = seen.insert((candidate.major, candidate.minor));
    }
    let latest_minor = candidates
        .iter()
        .filter(|candidate| !candidate.prerelease)
        .map(|candidate| candidate.minor)
        .max()
        .ok_or_else(|| "uv did not report a stable CPython interpreter".to_string())?;
    let preferred_minor = i128::from(latest_minor) - 2;
    candidates.sort_by(|left, right| final_order(left, right, preferred_minor, prefer_managed));
    Ok(())
}

fn source_order(left: &Candidate, right: &Candidate, prefer_managed: bool) -> Ordering {
    if prefer_managed {
        right.uv_python.cmp(&left.uv_python)
    } else {
        left.uv_python.cmp(&right.uv_python)
    }
}

fn initial_order(left: &Candidate, right: &Candidate, prefer_managed: bool) -> Ordering {
    left.prerelease
        .cmp(&right.prerelease)
        .then_with(|| source_order(left, right, prefer_managed))
        .then_with(|| right.major.cmp(&left.major))
        .then_with(|| right.minor.cmp(&left.minor))
        .then_with(|| right.patch.cmp(&left.patch))
}

fn final_order(
    left: &Candidate,
    right: &Candidate,
    preferred_minor: i128,
    prefer_managed: bool,
) -> Ordering {
    left.prerelease
        .cmp(&right.prerelease)
        .then_with(|| source_order(left, right, prefer_managed))
        .then_with(|| right.latest_patch.cmp(&left.latest_patch))
        .then_with(|| {
            right
                .preference_score(preferred_minor)
                .cmp(&left.preference_score(preferred_minor))
        })
        .then_with(|| (right.major == 3).cmp(&(left.major == 3)))
        .then_with(|| right.minor.cmp(&left.minor))
        .then_with(|| right.patch.cmp(&left.patch))
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::PythonVersions;

    fn versions() -> PythonVersions {
        let rows = [
            row("3.15.0a5", 3, 15, 0, true),
            row("3.14.3", 3, 14, 3, true),
            row("3.13.12", 3, 13, 12, true),
            row("3.12.12", 3, 12, 12, true),
            row("3.12.11", 3, 12, 11, true),
            row("3.11.14", 3, 11, 14, true),
            row("3.10.19", 3, 10, 19, true),
            row("3.9.25", 3, 9, 25, true),
        ];
        PythonVersions::parse(serde_json::to_vec(&rows).unwrap().as_slice(), true).unwrap()
    }

    fn row(
        version: &str,
        major: u64,
        minor: u64,
        patch: u64,
        downloadable: bool,
    ) -> serde_json::Value {
        json!({
            "version": version,
            "version_parts": {"major": major, "minor": minor, "patch": patch},
            "path": null,
            "symlink": null,
            "url": downloadable.then_some("https://example.invalid/python.tar.zst"),
            "variant": "default",
            "implementation": "cpython"
        })
    }

    #[test]
    fn preserves_reticulates_default_preference() {
        assert_eq!(versions().resolve(&[]).unwrap(), "3.12.12");
    }

    #[test]
    fn respects_system_python_preference() {
        let rows = [
            row("3.12.12", 3, 12, 12, true),
            row("3.11.14", 3, 11, 14, false),
        ];
        let output = serde_json::to_vec(&rows).unwrap();
        let managed = PythonVersions::parse(&output, true).unwrap();
        let system = PythonVersions::parse(&output, false).unwrap();

        assert_eq!(managed.resolve(&[]).unwrap(), "3.12.12");
        assert_eq!(system.resolve(&[]).unwrap(), "3.11.14");
    }

    #[test]
    fn applies_reticulate_style_version_constraints() {
        let versions = versions();
        for (constraints, expected) in [
            (&["3"], "3.12.12"),
            (&["3.11"], "3.11.14"),
            (&[">=3.9,<3.13"], "3.12.12"),
            (&["<=3.11"], "3.11.14"),
            (&[">3.11"], "3.12.12"),
            (&["!=3.12"], "3.11.14"),
            (&["==3.15.0a5"], "3.15.0a5"),
            (&["!=3.15.0a5"], "3.12.12"),
            (&[">=3.14.0a1"], "3.14.3"),
        ] {
            let constraints = constraints
                .iter()
                .map(|constraint| (*constraint).to_string())
                .collect::<Vec<_>>();
            assert_eq!(versions.resolve(&constraints).unwrap(), expected);
        }
    }

    #[test]
    fn reflects_an_exact_prerelease() {
        assert_eq!(
            versions().resolve(&["3.15.0a5".to_string()]).unwrap(),
            "3.15.0a5"
        );
    }

    #[test]
    fn reports_unsatisfied_constraints_with_available_versions() {
        let error = versions().resolve(&["<3".to_string()]).unwrap_err();
        assert!(error.contains("constraints: \"<3\""), "{error}");
        assert!(error.contains("3.12.12, 3.11.14"), "{error}");
    }
}
