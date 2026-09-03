#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite

PLATFORMS = {"darwin"}


def test_default_sandbox_supports_r_core_detection(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        cores <- parallel::detectCores()
        stopifnot(
          length(cores) == 1L,
          !is.na(cores),
          cores >= 1L
        )
        writeLines("R core detection available")
        """)
    client.send(r=r)
    return client._finish()


def test_applies_complete_expressions_before_incomplete_source(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        answer <- 42
        answer + (
        """)
    client.send(r=r)
    client.send(r="answer")
    # fmt: r
    r = code(r"""
        answer <- 43
        )
        """)
    client.send(r=r)
    client.send(r="answer")
    return client._finish()


def test_runs_native_top_level_bookkeeping(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        invisible(addTaskCallback(
          local({
            first <- TRUE
            function(expr, ...) {
              if (first) {
                first <<- FALSE
                return(TRUE)
              }
              cat(deparse1(expr), "\n", sep = "")
              FALSE
            }
          }),
          name = "mcp-console-test"
        ))
        mcp_console_callback_probe <- 42
        """)
    client.send(r=r)
    # fmt: r
    r = code(r"""
        warning("careful", call. = FALSE)
        invisible(42)
        cat("last value: ", identical(base::.Last.value, 42), "\n", sep = "")
        """)
    client.send(r=r)
    return client._finish()


def test_preserves_native_stack_and_last_value_binding(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        user_calls <- function() {
          vapply(sys.calls(), deparse1, character(1))
        }
        calls <- user_calls()
        cat("contains user call: ", "user_calls()" %in% calls, "\n", sep = "")
        cat(
          "contains internal call: ",
          any(grepl("mcp_console|base::get", calls)),
          "\n",
          sep = ""
        )
        cat(
          "global binding: ",
          exists(".Last.value", envir = globalenv(), inherits = FALSE),
          "\n",
          sep = ""
        )
        """)
    client.send(r=r)
    return client._finish()


if __name__ == "__main__":
    run_this_suite(__file__)
