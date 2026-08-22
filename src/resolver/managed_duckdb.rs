use std::process::Stdio;

use serde::Serialize;

use super::process::{
    ResolverProcess, ResolverStopHandle, read_output, resolver_command, stop_resolver, write_input,
};

const MANAGED_DUCKDB_EXTENSION_RESOLVER_SOURCE: &str = include_str!("programs/duckdb_extensions.R");

#[derive(Serialize)]
struct ResolverInput<'a> {
    extensions: &'a [String],
}

pub(crate) fn resolve_duckdb_extensions(
    managed_r: &super::ManagedR,
    extensions: &[String],
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<(), String> {
    let input = serde_json::to_vec(&ResolverInput { extensions })
        .expect("DuckDB extension resolver input should serialize as JSON");

    let rscript = managed_r.rscript();
    let mut command = resolver_command(rscript);
    command
        .args(["--vanilla", "-e", MANAGED_DUCKDB_EXTENSION_RESOLVER_SOURCE])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    managed_r.configure_resolver(&mut command)?;
    // DuckDB performs its normal extension installation outside the sandbox.
    // Names are JSON input, never R or SQL source.
    let mut child = command.spawn().map_err(|error| {
        format!(
            "failed to run DuckDB extension resolver with `{}`: {error}",
            rscript.display()
        )
    })?;
    let stdout = read_output(child.stdout.take().expect("resolver stdout is piped"));
    let stderr = read_output(child.stderr.take().expect("resolver stderr is piped"));
    let stdin = child.stdin.take().expect("resolver stdin is piped");
    let resolver = ResolverProcess::new();
    if let Err(error) = on_started(resolver.stop_handle()) {
        let _ = stop_resolver(&mut child, rscript, "DuckDB extension");
        return Err(error);
    }
    resolver.watch_exit(child.id());
    let output = resolver.wait(
        &mut child,
        write_input(stdin, input),
        stdout,
        stderr,
        rscript,
        "DuckDB extension",
    )?;
    if !output.status.success() {
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        let detail = if stderr.trim().is_empty() {
            stdout.trim()
        } else {
            stderr.trim()
        };
        return Err(format!(
            "DuckDB extension resolution failed with {}: {detail}",
            output.status
        ));
    }
    output
        .write_result
        .map_err(|error| format!("failed to write DuckDB extension requirements: {error}"))?;
    Ok(())
}
