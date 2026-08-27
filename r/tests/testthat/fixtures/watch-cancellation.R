args <- commandArgs(trailingOnly = TRUE)
stopifnot(length(args) == 4L)

workspace <- args[[1L]]
marker <- args[[2L]]
parent <- as.integer(args[[3L]])
ack <- args[[4L]]
deadline <- Sys.time() + 15

repeat {
  transcripts <- Sys.glob(file.path(
    workspace,
    ".mcp-console",
    "sessions",
    "*",
    "transcript.md"
  ))
  if (length(transcripts) == 1L) {
    transcript <- paste(readLines(transcripts, warn = FALSE), collapse = "\n")
    marker_at <- regexpr(marker, transcript, fixed = TRUE)[[1L]]
    if (marker_at > 0L) {
      after_marker <- substring(transcript, marker_at + nchar(marker))
      poll_at <- regexpr("## Call [0-9]+: Poll", after_marker)[[1L]]
      if (poll_at > 0L) {
        after_poll <- substring(after_marker, poll_at)
        if (grepl('"timeout_ms": 60000', after_poll, fixed = TRUE)) {
          stopifnot(tools::pskill(parent, tools::SIGINT))
          stopifnot(file.create(ack))
          quit(status = 0L)
        }
      }
    }
  }

  if (Sys.time() >= deadline) {
    stop("timed out waiting for the marked result and blocking poll")
  }
  Sys.sleep(0.01)
}
