use std::path::Path;
use std::process::Stdio;

use serde::Serialize;

use super::process::{
    ResolverProcess, ResolverStopHandle, read_output, resolver_command, write_input,
};

const SOURCE: &str = include_str!("programs/duckdb_extensions.py");

#[derive(Serialize)]
struct ResolverInput<'a> {
    extensions: &'a [String],
    extension_directory: &'a Path,
}

pub(crate) fn resolve_python_duckdb_extensions(
    managed_python: &super::ManagedPython,
    extensions: &[String],
    extension_directory: &Path,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<(), String> {
    let input = serde_json::to_vec(&ResolverInput {
        extensions,
        extension_directory,
    })
    .expect("DuckDB extension resolver input should serialize as JSON");

    let python = managed_python.python();
    let mut command = resolver_command(python);
    command
        .args(["-I", "-c", SOURCE])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn().map_err(|error| {
        format!(
            "failed to run DuckDB extension resolver with `{}`: {error}",
            python.display()
        )
    })?;
    let stdout = read_output(child.stdout.take().expect("resolver stdout is piped"));
    let stderr = read_output(child.stderr.take().expect("resolver stderr is piped"));
    let stdin = child.stdin.take().expect("resolver stdin is piped");
    let resolver = ResolverProcess::new();
    resolver.watch_exit(child.id());
    if let Err(error) = on_started(resolver.stop_handle()) {
        resolver
            .abort(&mut child, python, "DuckDB extension")
            .map_err(|cleanup| format!("{error}; {cleanup}"))?;
        return Err(error);
    }
    let output = resolver.wait(
        &mut child,
        write_input(stdin, input),
        stdout,
        stderr,
        python,
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
