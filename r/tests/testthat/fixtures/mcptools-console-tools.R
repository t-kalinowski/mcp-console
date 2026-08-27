send <- function(r = NULL) {
  if (identical(r, "interrupt()")) {
    parent <- as.integer(Sys.getenv("MCP_CONSOLE_TEST_PARENT"))
    gate <- Sys.getenv("MCP_CONSOLE_TEST_INTERRUPT_GATE")
    stopifnot(tools::pskill(parent, tools::SIGINT), nzchar(gate))

    # The outer interrupt handler creates this gate after cancellation is sent.
    deadline <- Sys.time() + 5
    while (!file.exists(gate)) {
      if (Sys.time() >= deadline) {
        stop("timed out waiting for the interrupt gate")
      }
      Sys.sleep(0.01)
    }
  }

  if (is.null(r)) "<poll>" else r
}

list(ellmer::tool(
  send,
  name = "send",
  description = "Persistent test console.",
  arguments = list(r = ellmer::type_string("R code", required = FALSE))
))
