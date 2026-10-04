base::local({
  input <- base::paste(
    base::readLines(
      base::file("stdin", encoding = "UTF-8"),
      warn = FALSE
    ),
    collapse = "\n"
  )
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
        # Local preparation and workers share one captured extension cache.
        # Remote preparation keeps DuckDB core's native default.
        extension_directory = Sys.getenv(
          "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY"
        ),
        secret_directory = base::file.path(storage, "stored-secrets"),
        temp_directory = base::file.path(storage, "spill")
      )
    )
  )
  base::on.exit(DBI::dbDisconnect(connection), add = TRUE)
  DBI::dbExecute(connection, "SET enable_progress_bar = false")
  if (Sys.getenv("CODEX_NETWORK_PROXY_ACTIVE") == "1") {
    # Extension INSTALL does not consume the proxy environment itself.
    DBI::dbExecute(
      connection,
      paste(
        "SET http_proxy =",
        DBI::dbQuoteString(connection, Sys.getenv("HTTP_PROXY"))
      )
    )
  }

  for (extension in extensions) {
    identifier <- DBI::dbQuoteIdentifier(connection, extension)
    DBI::dbExecute(
      connection,
      base::paste("INSTALL", identifier)
    )
  }
})
