#!/usr/bin/env -S uv run --script

from pathlib import Path
from textwrap import dedent

from _support import McpClient, Transcript, run_this_suite


PLATFORMS = {"darwin"}


def test_evaluates_a_complete_cell(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    # fmt: r
    r = dedent(r"""
        answer <- 40
        answer + 1
        answer + 2
        cat("done\n")
        invisible(99)
        """).strip()
    client.call_tool("send", r=r)

    # fmt: r
    r = dedent(r"""
        identical(
          as.vector(splines::splineDesign(
            knots = c(0, 0, 0, 0, 1, 1, 1, 1),
            x = 0.5
          )),
          c(0.125, 0.375, 0.375, 0.125)
        )
        """).strip()
    client.call_tool("send", r=r)
    client.call_tool("send", r='stop("boom", call. = FALSE)')
    client.call_tool("send", r="answer")
    client.call_tool("send", r="silent <- 1")
    return client.finish()


def test_routes_combined_and_followup_stdin(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()

    # fmt: r
    r = dedent(r"""
        paste("hello", readline("name> "))
        """).strip()
    client.call_tool("send", r=r, stdin="Ada\nstale\n")
    assert last_tool_text(client) == 'name>\n[1] "hello Ada"\n'

    # fmt: r
    r = dedent(r"""
        42
        """).strip()
    client.call_tool("send", r=r, stdin="unused\n")
    assert last_tool_text(client) == "[1] 42\n[stdin discarded]"

    # fmt: r
    r = dedent(r"""
        paste("color", readline("color> "))
        """).strip()
    client.call_tool("send", r=r)
    assert last_tool_text(client) == "color>\n[input]"
    client.call_tool("send", stdin="bl")
    assert last_tool_text(client) == "color>\n[input]"
    client.call_tool("send", stdin="ue\n")
    assert last_tool_text(client) == '[1] "color blue"\n'
    return client.finish()


def last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["output"]["result"]
    assert result["isError"] is False, result
    assert result["content"] == [{"type": "text", "text": result["content"][0]["text"]}]
    return result["content"][0]["text"]


def test_applies_complete_expressions_before_incomplete_source(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    # fmt: r
    r = dedent(r"""
        answer <- 42
        answer + (
        """).strip()
    client.call_tool("send", r=r)
    client.call_tool("send", r="answer")
    # fmt: r
    r = dedent(r"""
        answer <- 43
        )
        """).strip()
    client.call_tool("send", r=r)
    client.call_tool("send", r="answer")
    return client.finish()


def test_runs_native_top_level_bookkeeping(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    # fmt: r
    r = dedent(r"""
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
        """).strip()
    client.call_tool("send", r=r)
    # fmt: r
    r = dedent(r"""
        warning("careful", call. = FALSE)
        invisible(42)
        cat("last value: ", identical(base::.Last.value, 42), "\n", sep = "")
        """).strip()
    client.call_tool("send", r=r)
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
