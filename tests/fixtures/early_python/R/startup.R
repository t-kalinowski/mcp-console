.onLoad <- function(libname, pkgname) {
  if (!interactive()) {
    return(invisible(NULL))
  }
  python <- Sys.getenv("MCP_CONSOLE_TEST_EARLY_PYTHON", unset = "")
  if (nzchar(python)) {
    Sys.setenv(RETICULATE_PYTHON = python)
  }
  reticulate::py_run_string(paste(
    "import json, os, subprocess, sys",
    "early_object = object()",
    "early_identity = id(early_object)",
    "early_executable = sys.executable",
    "early_prefixes = (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix)",
    "early_path = os.environ['PATH']",
    "early_child_program = \"import json, os, sys; print(json.dumps([sys.executable, sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix, os.environ['PATH']]))\"",
    "early_child = json.loads(subprocess.check_output([sys.executable, '-c', early_child_program], text=True))",
    sep = "\n"
  ))
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
  if (identical(Sys.getenv("MCP_CONSOLE_TEST_STARTUP_PLOTS"), "1")) {
    graphics::plot(1:3)
  }
}

.onAttach <- function(libname, pkgname) {
  if (!interactive()) {
    return(invisible(NULL))
  }
  if (identical(Sys.getenv("MCP_CONSOLE_TEST_STARTUP_PLOTS"), "1")) {
    graphics::plot(3:1)
  }
  ready <- Sys.getenv("MCP_CONSOLE_TEST_STARTUP_READY")
  if (interactive() && nzchar(ready)) {
    ready <- fifo(ready, open = "wb", blocking = TRUE)
    writeBin(charToRaw("1"), ready)
    close(ready)
    release <- fifo(
      Sys.getenv("MCP_CONSOLE_TEST_STARTUP_RELEASE"),
      open = "rb",
      blocking = TRUE
    )
    stopifnot(identical(readBin(release, "raw", 1L), charToRaw("1")))
    close(release)
  }
}

py <- sql_connection <- console_sql_connection <- function(...) {
  stop("startup package shadowed Console tools")
}
