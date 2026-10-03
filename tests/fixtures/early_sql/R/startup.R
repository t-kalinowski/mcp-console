.onLoad <- function(libname, pkgname) {
  if (!interactive()) {
    return(invisible())
  }
  if (nzchar(Sys.getenv("MCP_CONSOLE_TEST_SQL_STARTED"))) {
    observed <- FALSE
    observe_connection <- function(connection) {
      stopifnot(DBI::dbIsValid(connection))
      DBI::dbExecute(
        connection,
        "CREATE TABLE IF NOT EXISTS startup_catalog AS SELECT 42 AS answer"
      )
      # DuckDB may use a temporary connection while constructing the driver.
      # Gate the first actual DBI connection and mark every returned catalog.
      if (!observed) {
        observed <<- TRUE
        signal <- fifo(
          Sys.getenv("MCP_CONSOLE_TEST_SQL_STARTED"),
          open = "wb",
          blocking = TRUE
        )
        writeBin(charToRaw("1"), signal)
        close(signal)
        gate <- fifo(
          Sys.getenv("MCP_CONSOLE_TEST_SQL_RELEASE"),
          open = "rb",
          blocking = TRUE
        )
        stopifnot(identical(readBin(gate, "raw", 1L), charToRaw("1")))
        close(gate)
      }
    }
    trace(
      "dbConnect",
      where = asNamespace("DBI"),
      print = FALSE,
      exit = bquote(.(observe_connection)(returnValue()))
    )
  }
}
