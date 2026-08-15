use std::process::Stdio;

use serde::Serialize;

use super::process::{
    ResolverProcess, ResolverStopHandle, read_output, resolver_command, stop_resolver, write_input,
};

const DUCKDB_EXTENSION_RESOLVER: &str = r#"
base::local({
  input <- base::paste(base::readLines(
    base::file("stdin", encoding = "UTF-8"),
    warn = FALSE
  ), collapse = "\n")
  input <- jsonlite::fromJSON(input)
  extensions <- base::unlist(input$extensions, use.names = FALSE)
  base::stopifnot(
    base::is.character(extensions),
    base::length(extensions) > 0L,
    base::all(base::grepl("^[a-z][a-z0-9_]*$", extensions))
  )

  storage <- base::tempfile("mcp-console-duckdb-resolver-")
  connection <- DBI::dbConnect(
    duckdb::duckdb(
      dbdir = ":memory:",
      config = list(
        # Suppress DuckDB-R's storage policy while leaving DuckDB core to use
        # its compiled default extension directory.
        extension_directory = "",
        secret_directory = base::file.path(storage, "stored-secrets"),
        temp_directory = base::file.path(storage, "spill")
      )
    )
  )
  base::on.exit(DBI::dbDisconnect(connection), add = TRUE)
  DBI::dbExecute(connection, "SET enable_progress_bar = false")

  for (extension in extensions) {
    identifier <- DBI::dbQuoteIdentifier(connection, extension)
    DBI::dbExecute(
      connection,
      base::paste("INSTALL", identifier)
    )
  }
})
"#;

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
        .args(["--vanilla", "-e", DUCKDB_EXTENSION_RESOLVER])
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
