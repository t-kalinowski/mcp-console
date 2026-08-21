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
