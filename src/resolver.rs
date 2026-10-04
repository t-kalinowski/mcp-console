#[derive(Clone, Copy, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
pub(crate) enum ResolverControlOutcome {
    Interrupted,
    Cancelled,
}

#[cfg(unix)]
pub(crate) mod broker;
pub(crate) mod data;
mod environment;
pub(crate) use environment::{ManagedPython, ManagedR};
pub(crate) mod execution;
pub(crate) mod policy;
pub(crate) mod preparation;
#[cfg(unix)]
mod storage;
#[cfg(unix)]
pub(crate) mod workload;

#[cfg(any(unix, windows))]
mod managed_duckdb;
#[cfg(any(unix, windows))]
mod managed_duckdb_python;
#[cfg(any(unix, windows))]
mod managed_python;
#[cfg(any(unix, windows))]
mod managed_r;
#[cfg(any(unix, windows))]
pub(crate) mod process;
#[cfg(any(unix, windows))]
mod python_configuration;
#[cfg(any(unix, windows))]
mod python_version;
#[cfg(any(unix, windows))]
mod r_program;
pub(crate) mod result_file;
#[cfg(not(any(unix, windows)))]
mod unsupported;

pub(crate) fn find_path_entry(program: &str) -> Option<std::path::PathBuf> {
    let path = std::env::var_os("PATH")?;
    // A broken symlink or non-executable entry is a broken installation, not
    // permission to select a different resolver.
    std::env::split_paths(&path).find_map(|directory| {
        let candidate = if directory.as_os_str().is_empty() {
            std::path::PathBuf::from(".").join(program)
        } else {
            directory.join(program)
        };
        #[cfg(windows)]
        let candidate = if candidate.extension().is_none() {
            candidate.with_extension("exe")
        } else {
            candidate
        };
        std::fs::symlink_metadata(&candidate)
            .is_ok()
            .then_some(candidate)
    })
}

#[cfg(any(unix, windows))]
pub(crate) use python_configuration::ManagedPythonResolverConfiguration;

#[cfg(any(unix, windows))]
pub(crate) use managed_duckdb::resolve_duckdb_extensions;
#[cfg(any(unix, windows))]
pub(crate) use managed_duckdb_python::resolve_python_duckdb_extensions;
#[cfg(any(unix, windows))]
pub(crate) use managed_python::{
    resolve_python_manifest_for_remote, resolve_python_version, resolve_python_version_for_remote,
};
#[cfg(any(unix, windows))]
pub(crate) use managed_r::{
    ManagedRBootstrap, ManagedRResolverConfiguration, discover, resolve_r, resolve_r_with,
};
#[cfg(any(unix, windows))]
pub(crate) use process::{ResolverControl, ResolverStopHandle};
#[cfg(not(any(unix, windows)))]
pub(crate) use unsupported::{
    ManagedPython, ManagedR, ManagedRBootstrap, ManagedRResolverConfiguration, ResolverStopHandle,
    resolve_duckdb_extensions, resolve_python, resolve_python_manifest, resolve_python_version,
    resolve_r, resolve_r_with,
};
