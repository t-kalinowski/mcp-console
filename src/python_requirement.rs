use pep508_rs::{Requirement, VerbatimUrl, VersionOrUrl};

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

#[cfg(test)]
mod tests {
    use super::validate;

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
}
