.onAttach <- function(libname, pkgname) {
  if (!interactive()) {
    return(invisible(NULL))
  }
  ready <- fifo(
    Sys.getenv("MCP_CONSOLE_TEST_BOOTSTRAP_REACHED"),
    "wb",
    blocking = TRUE
  )
  writeBin(charToRaw("1"), ready)
  close(ready)
  release <- fifo(
    Sys.getenv("MCP_CONSOLE_TEST_BOOTSTRAP_RELEASE"),
    "rb",
    blocking = TRUE
  )
  stopifnot(identical(readBin(release, "raw", 1L), charToRaw("1")))
  close(release)
  script <- Sys.getenv("MCP_CONSOLE_TEST_BOOTSTRAP_SCRIPT")
  if (nzchar(script)) {
    sys.source(script, envir = globalenv())
  } else {
    assign("bootstrap_value", readline("R startup> "), envir = globalenv())
  }
}
