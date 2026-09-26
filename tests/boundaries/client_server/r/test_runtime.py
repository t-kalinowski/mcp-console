#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_detects_cpu_cores(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
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
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_rejects_incomplete_and_invalid_source(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.send(r="answer <- 41")
    # fmt: r
    r = code(r"""
        answer <- 42
        created <- TRUE
        answer + (
        """)
    client.send(r=r)
    assert "unexpected end of input" in last_tool_text(client), last_tool_text(client)
    assert last_tool_text(client).startswith("Error: "), last_tool_text(client)
    assert client.transcript[-1]["result"]["isError"] is False
    # fmt: r
    unchanged = code(r"""
        stopifnot(
          identical(answer, 41),
          identical(base::.Last.value, 41),
          !exists("created", envir = globalenv(), inherits = FALSE)
        )
        answer
        """)
    client.send(r=unchanged)
    assert last_tool_text(client) == "[1] 41\n"
    client.send(r=")")
    assert "unexpected ')'" in last_tool_text(client)
    assert last_tool_text(client).startswith("Error: "), last_tool_text(client)
    # fmt: r
    r = code(r"""
        answer <- 43
        created <- TRUE
        )
        """)
    client.send(r=r)
    assert "unexpected ')'" in last_tool_text(client)
    assert last_tool_text(client).startswith("Error: "), last_tool_text(client)
    assert client.transcript[-1]["result"]["isError"] is False
    client.send(r=unchanged)
    assert last_tool_text(client) == "[1] 41\n"
    # This parser failure raises an R condition instead of returning PARSE_ERROR.
    # fmt: r
    r = code(r"""
        answer <- 44
        created <- TRUE
        function(x, x) x
        """)
    client.send(r=r)
    assert "repeated formal argument 'x'" in last_tool_text(client)
    assert client.transcript[-1]["result"]["isError"] is False
    client.send(r=unchanged)
    assert last_tool_text(client) == "[1] 41\n"
    # A runtime error still preserves expressions evaluated before it.
    # fmt: r
    r = code(r"""
        answer <- 45
        stop("runtime failure")
        answer <- 46
        """)
    client.send(r=r)
    assert last_tool_text(client) == "Error: runtime failure\n"
    client.send(r="answer")
    assert last_tool_text(client) == "[1] 45\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_rejects_source_without_error_side_effects(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # Seed a real traceback and verify the error hook still handles runtime errors.
    # fmt: r
    r = code(r"""
        error_hook_calls <- 0L
        options(error = quote({
          error_hook_calls <<- error_hook_calls + 1L
          traceback()
        }))
        f <- function() stop("seed traceback")
        f()
        """)
    client.send(r=r)
    client.send(r="traceback()")
    previous_traceback = last_tool_text(client)
    assert 'stop("seed traceback")' in previous_traceback
    # fmt: r
    incomplete = code(r"""
        created <- TRUE
        (
        """)
    # fmt: r
    invalid = code(r"""
        created <- TRUE
        )
        """)
    # fmt: r
    duplicate_formal = code(r"""
        created <- TRUE
        function(x, x) x
        """)
    for source in (incomplete, invalid, duplicate_formal):
        client.send(r=source)
        assert last_tool_text(client).startswith("Error: "), last_tool_text(client)
        assert 'stop("seed traceback")' not in last_tool_text(client), last_tool_text(
            client
        )
        client.send(r="traceback()")
        assert last_tool_text(client) == previous_traceback, last_tool_text(client)
    # fmt: r
    r = code(r"""
        stopifnot(
          error_hook_calls == 1L,
          !exists("created", envir = globalenv(), inherits = FALSE)
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]", last_tool_text(client)
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_preserves_parser_warning_behavior(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.send(r="invisible(1.0L)")
    assert last_tool_text(client).count("unnecessary decimal point") == 1
    # fmt: r
    r = code(r"""
        options(warning.expression = quote(cat("parser warning\n")))
        """)
    client.send(r=r)
    client.send(r="invisible(1.0L)")
    assert last_tool_text(client) == "parser warning\n", last_tool_text(client)
    # fmt: r
    r = code(r"""
        options(
          warning.expression = NULL,
          warn = 2,
          error = quote(cat("error handler warn: ", getOption("warn"), "\n", sep = ""))
        )
        """)
    client.send(r=r)
    # fmt: r
    r = code(r"""
        function(x, x) x
        """)
    client.send(r=r)
    assert "repeated formal argument 'x'" in last_tool_text(client)
    assert "error handler warn:" not in last_tool_text(client)
    # The warning becomes an error only when the native REPL reaches the literal.
    # fmt: r
    r = code(r"""
        answer <- 42
        1.0L
        """)
    client.send(r=r)
    assert "(converted from warning)" in last_tool_text(client)
    assert last_tool_text(client).endswith("error handler warn: 2\n")
    client.send(r="answer")
    assert last_tool_text(client) == "[1] 42\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_preserves_parser_warning_handlers(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # The native REPL's R_ToplevelExec isolates condition handlers between cells.
    # Register the handler in the cell where the native parser will warn.
    # fmt: r
    r = code(r"""
        parser_warnings <- 0L
        globalCallingHandlers(warning = function(w) {
          parser_warnings <<- parser_warnings + 1L
          cat("global warning handler\n")
          invokeRestart("muffleWarning")
        })
        invisible(1.0L)
        """)
    client.send(r=r)
    assert last_tool_text(client) == "global warning handler\n", last_tool_text(client)
    client.send(r="parser_warnings")
    assert last_tool_text(client) == "[1] 1\n"
    # An error from the handler must occur after the preceding assignment.
    # fmt: r
    r = code(r"""
        globalCallingHandlers(NULL)
        globalCallingHandlers(warning = function(w) {
          stop("global warning handler", call. = FALSE)
        })
        answer <- 42
        1.0L
        """)
    client.send(r=r)
    assert last_tool_text(client) == "Error: global warning handler\n"
    client.send(r="answer")
    assert last_tool_text(client) == "[1] 42\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_runs_native_top_level_bookkeeping(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
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
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"
    client.send(r="# No expression to evaluate.")
    assert last_tool_text(client) == "[done]"
    # Neither preflight nor a rejected cell may consume the registered callback.
    # fmt: r
    r = code(r"""
        mcp_console_callback_probe <- 99
        (
        """)
    client.send(r=r)
    assert "unexpected end of input" in last_tool_text(client), last_tool_text(client)
    client.send(r="mcp_console_callback_probe <- 42")
    assert last_tool_text(client) == "mcp_console_callback_probe <- 42\n"
    # fmt: r
    r = code(r"""
        warning("careful", call. = FALSE)
        invisible(42)
        cat("last value: ", identical(base::.Last.value, 42), "\n", sep = "")
        """)
    client.send(r=r)
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_preserves_native_stack_and_last_value_binding(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # The private parser must not resolve helpers from the user's workspace.
    # fmt: r
    r = code(r"""
        str2expression <- suppressWarnings <- function(...) stop("masked parser")
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"
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
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
