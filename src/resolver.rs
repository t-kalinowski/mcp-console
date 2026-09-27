#[derive(Clone, Copy, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
pub(crate) enum ResolverControlOutcome {
    Interrupted,
    Cancelled,
}

pub(crate) mod broker;
pub(crate) mod data;
pub(crate) mod execution;
mod policy;
pub(crate) mod preparation;
pub(crate) mod result_file;
mod storage;
pub(crate) mod workload;

#[cfg(unix)]
mod managed_duckdb;
#[cfg(unix)]
mod managed_python;
#[cfg(unix)]
mod managed_r;
#[cfg(unix)]
pub(crate) mod process;
mod python_configuration;
#[cfg(unix)]
mod python_version;
#[cfg(not(unix))]
mod unsupported;

pub(crate) fn find_path_entry(program: &str) -> Option<std::path::PathBuf> {
    let path = std::env::var_os("PATH")?;
    // A broken symlink or non-executable entry is a broken installation, not
    // permission to select a different resolver.
    std::env::split_paths(&path)
        .map(|directory| {
            if directory.as_os_str().is_empty() {
                std::path::PathBuf::from(".").join(program)
            } else {
                directory.join(program)
            }
        })
        .find(|candidate| std::fs::symlink_metadata(candidate).is_ok())
}

pub(crate) use python_configuration::ManagedPythonResolverConfiguration;

#[cfg(unix)]
pub(crate) use managed_duckdb::resolve_duckdb_extensions;
#[cfg(unix)]
pub(crate) use managed_python::{ManagedPython, resolve_python_manifest, resolve_python_version};
#[cfg(unix)]
pub(crate) use managed_r::{
    ManagedR, ManagedRBootstrap, ManagedRResolverConfiguration, discover, resolve_r_with,
};
#[cfg(unix)]
pub(crate) use process::{ResolverControl, ResolverStopHandle};
#[cfg(not(unix))]
pub(crate) use unsupported::{
    ManagedPython, ManagedR, ManagedRBootstrap, ManagedRResolverConfiguration, ResolverStopHandle,
    resolve_duckdb_extensions, resolve_python, resolve_python_manifest, resolve_python_version,
    resolve_r, resolve_r_with,
};
