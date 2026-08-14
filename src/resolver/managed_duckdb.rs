use std::os::unix::process::CommandExt as _;
use std::path::Path;
use std::process::{Command, Stdio};

use serde::Serialize;

use super::process::{
    ResolverProcess, ResolverStopHandle, read_output, stop_resolver, write_input,
};

const DUCKDB_EXTENSION_RESOLVER: &str = r#"
base::local({
  input <- base::paste(base::readLines(
    base::file("stdin", encoding = "UTF-8"),
    warn = FALSE
  ), collapse = "\n")
  input <- jsonlite::fromJSON(input)
  extensions <- base::unlist(input$extensions, use.names = FALSE)
  extension_directory <- input$extension_directory
  base::stopifnot(
    base::is.character(extensions),
    base::length(extensions) > 0L,
    base::all(base::grepl("^[a-z][a-z0-9_]*$", extensions)),
    base::is.character(extension_directory),
    base::length(extension_directory) == 1L,
    base::nzchar(extension_directory)
  )

  storage <- base::tempfile("mcp-console-duckdb-resolver-")
  connection <- DBI::dbConnect(
    duckdb::duckdb(
      dbdir = ":memory:",
      config = list(
        extension_directory = extension_directory,
        secret_directory = base::file.path(storage, "stored-secrets"),
        temp_directory = base::file.path(storage, "spill"),
        autoinstall_known_extensions = "false",
        autoload_known_extensions = "false"
      ),
      environment_scan = FALSE
    )
  )
  base::on.exit(DBI::dbDisconnect(connection), add = TRUE)
  DBI::dbExecute(connection, "SET enable_progress_bar = false")

  known <- DBI::dbGetQuery(
    connection,
    "SELECT extension_name FROM duckdb_extensions()"
  )$extension_name
  unknown <- base::setdiff(extensions, known)
  if (base::length(unknown)) {
    base::stop(
      "unknown core DuckDB extension: ",
      base::paste(unknown, collapse = ", "),
      call. = FALSE
    )
  }

  for (extension in extensions) {
    identifier <- DBI::dbQuoteIdentifier(connection, extension)
    DBI::dbExecute(
      connection,
      base::paste("INSTALL", identifier, "FROM core")
    )
  }
})
"#;

#[derive(Serialize)]
struct ResolverInput<'a> {
    extensions: &'a [String],
    extension_directory: &'a str,
}

pub(crate) fn resolve_duckdb_extensions(
    managed_r: &super::ManagedR,
    extensions: &[String],
    extension_directory: &Path,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<(), String> {
    let extension_directory = extension_directory
        .to_str()
        .ok_or_else(|| "DuckDB extension directory path is not UTF-8".to_string())?;
    let input = serde_json::to_vec(&ResolverInput {
        extensions,
        extension_directory,
    })
    .expect("DuckDB extension resolver input should serialize as JSON");

    let rscript = managed_r.rscript();
    let mut command = Command::new(rscript);
    command
        .args(["--vanilla", "-e", DUCKDB_EXTENSION_RESOLVER])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    managed_r.configure_resolver(&mut command)?;
    // DuckDB performs its normal core-extension installation outside the
    // sandbox. Names and the cache path are JSON input, never R or SQL source.
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
