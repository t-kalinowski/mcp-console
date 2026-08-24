use std::collections::BTreeSet;
use std::ffi::OsString;

use super::requirements::push_duckdb_r_target;

pub(in crate::worker_client) struct Environment {
    pub(in crate::worker_client) custom_worker: bool,
    pub(in crate::worker_client) duckdb_extensions: BTreeSet<String>,
    /// R libraries that may have supplied DuckDB in the current worker generation.
    pub(in crate::worker_client) duckdb_r_targets: Vec<crate::resolver::ManagedR>,
    pub(in crate::worker_client) python: Option<PythonEnvironment>,
    pub(in crate::worker_client) r: Option<crate::resolver::ManagedR>,
}

const USER_SELECTED_PYTHON_ERROR: &str = "managed Python requirements are disabled because the session uses a user-selected Python environment";

#[derive(Clone)]
pub(in crate::worker_client) enum PythonEnvironment {
    Managed {
        selected: crate::resolver::ManagedPython,
        resolver: crate::resolver::ManagedPythonResolverConfiguration,
    },
    UserSelected(OsString),
}

impl PythonEnvironment {
    pub(in crate::worker_client) fn builtin(
        configured: Option<OsString>,
        resolver: crate::resolver::ManagedPythonResolverConfiguration,
        managed_r: Option<&crate::resolver::ManagedR>,
    ) -> Result<Self, String> {
        if let Some(configured) = configured
            && !configured.is_empty()
            && configured != "managed"
        {
            return Ok(Self::UserSelected(configured));
        }
        let selected = crate::resolver::resolve_python(&[], &resolver, managed_r, |_| Ok(()))?;
        Ok(Self::Managed { selected, resolver })
    }

    pub(super) fn managed(&self) -> Option<&crate::resolver::ManagedPython> {
        match self {
            Self::Managed { selected, .. } => Some(selected),
            Self::UserSelected(_) => None,
        }
    }

    pub(super) fn managed_parts(
        &self,
    ) -> Result<
        (
            &crate::resolver::ManagedPython,
            &crate::resolver::ManagedPythonResolverConfiguration,
        ),
        String,
    > {
        match self {
            Self::Managed { selected, resolver } => Ok((selected, resolver)),
            Self::UserSelected(_) => Err(USER_SELECTED_PYTHON_ERROR.to_string()),
        }
    }

    pub(in crate::worker_client) fn replace_managed(
        &mut self,
        managed: crate::resolver::ManagedPython,
    ) -> Result<(), String> {
        match self {
            Self::Managed { selected, .. } => {
                *selected = managed;
                Ok(())
            }
            Self::UserSelected(_) => Err(USER_SELECTED_PYTHON_ERROR.to_string()),
        }
    }

    #[cfg(target_os = "macos")]
    pub(in crate::worker_client) fn configure_worker(
        &self,
        command: &mut crate::sandbox::SandboxedCommand,
    ) {
        match self {
            Self::Managed { selected, .. } => selected.configure_worker(command),
            Self::UserSelected(python) => {
                command
                    .env("RETICULATE_PYTHON", python)
                    .env_remove("MCP_CONSOLE_MANAGED_PYTHON");
            }
        }
    }
}

pub(super) fn ensure_python_additions_available(
    environment: &Environment,
    additions: &[String],
) -> Result<(), String> {
    if additions.is_empty() {
        return Ok(());
    }
    if environment.custom_worker {
        return Err("Python requirements are unavailable with a custom worker".to_string());
    }
    match environment.python.as_ref() {
        Some(PythonEnvironment::Managed { .. }) => Ok(()),
        Some(PythonEnvironment::UserSelected(_)) => Err(USER_SELECTED_PYTHON_ERROR.to_string()),
        None => Err("managed Python environment is unavailable".to_string()),
    }
}

pub(super) fn commit_managed_r(
    environment: &mut Environment,
    managed_r: crate::resolver::ManagedR,
) {
    push_duckdb_r_target(&mut environment.duckdb_r_targets, managed_r.clone());
    environment.r = Some(managed_r);
}
