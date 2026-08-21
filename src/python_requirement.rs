use pep508_rs::{
    Requirement, VerbatimUrl, VersionOrUrl,
    pep440_rs::{Operator, Version, VersionSpecifier},
};

pub(crate) fn validate(requirement: &str) -> Result<(), String> {
    let parsed = requirement.parse::<Requirement<VerbatimUrl>>();
    if !matches!(
        parsed,
        Ok(Requirement {
            version_or_url: None | Some(VersionOrUrl::VersionSpecifier(_)),
            ..
        })
    ) {
        return Err(format!(
            "Python requirement `{requirement}` is not accepted: host-side managed resolution accepts named package requirements only"
        ));
    }
    Ok(())
}

pub(crate) fn validate_all(requirements: &[String]) -> Result<(), String> {
    requirements
        .iter()
        .try_for_each(|requirement| validate(requirement))
}

pub(crate) fn validate_version_constraint(constraint: &str) -> Result<(), String> {
    let is_supported = constraint.split(',').all(|clause| {
        let clause = clause.trim();
        clause.parse::<Version>().is_ok()
            || clause.parse::<VersionSpecifier>().is_ok_and(|specifier| {
                matches!(
                    specifier.operator(),
                    Operator::Equal
                        | Operator::EqualStar
                        | Operator::NotEqual
                        | Operator::NotEqualStar
                        | Operator::LessThan
                        | Operator::LessThanEqual
                        | Operator::GreaterThan
                        | Operator::GreaterThanEqual
                )
            })
    });
    if !constraint.is_empty() && is_supported {
        return Ok(());
    }
    Err(format!(
        "Python version constraint `{constraint}` is not accepted: host-side managed resolution accepts version numbers and supported PEP 440 version specifiers only"
    ))
}

pub(crate) fn validate_version_constraints(constraints: &[String]) -> Result<(), String> {
    constraints
        .iter()
        .try_for_each(|constraint| validate_version_constraint(constraint))
}

#[cfg(test)]
mod tests {
    use super::{validate, validate_version_constraint};

    #[test]
    fn accepts_named_registry_requirements() {
        for requirement in [
            "requests",
            "requests[socks]",
            "requests>=2,<3",
            "requests[socks]>=2; python_version >= '3.10'",
        ] {
            assert_eq!(validate(requirement), Ok(()), "{requirement}");
        }
    }

    #[test]
    fn rejects_direct_and_local_requirements() {
        for requirement in [
            "/tmp/project",
            "./project",
            "../project",
            "file:///tmp/project",
            "-e ./project",
            "example @ https://example.invalid/example.whl",
            "./example.whl",
            "example.whl",
            "example.tar.gz",
        ] {
            assert_eq!(
                validate(requirement),
                Err(format!(
                    "Python requirement `{requirement}` is not accepted: host-side managed resolution accepts named package requirements only"
                ))
            );
        }
    }

    #[test]
    fn accepts_python_versions_and_pep_440_specifiers() {
        for constraint in ["3.11", "3.14.0a3", ">=3.9", ">=3.9,<3.13", "==3.12.*"] {
            assert_eq!(
                validate_version_constraint(constraint),
                Ok(()),
                "{constraint}"
            );
        }
    }

    #[test]
    fn rejects_python_interpreter_selectors() {
        for constraint in [
            "",
            "/tmp/python",
            "./python",
            "../python",
            "file:///tmp/python",
            "/tmp/python-installation",
            "python3",
            "~=3.11",
            "===3.11",
        ] {
            assert_eq!(
                validate_version_constraint(constraint),
                Err(format!(
                    "Python version constraint `{constraint}` is not accepted: host-side managed resolution accepts version numbers and supported PEP 440 version specifiers only"
                ))
            );
        }
    }
}
