#!/usr/bin/env -S uv run --script

from pathlib import Path

from _support import McpClient, Transcript, code, run_this_suite


PLATFORMS = {"darwin"}


def test_evaluates_a_complete_cell(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        answer <- 40
        answer + 1
        answer + 2
        cat("done\n")
        invisible(99)
        """)
    client.call_tool("send", r=r)

    # fmt: r
    r = code(r"""
        identical(
          as.vector(splines::splineDesign(
            knots = c(0, 0, 0, 0, 1, 1, 1, 1),
            x = 0.5
          )),
          c(0.125, 0.375, 0.375, 0.125)
        )
        """)
    client.call_tool("send", r=r)
    client.call_tool("send", r='stop("boom")')
    client.call_tool("send", r="answer")
    client.call_tool("send", r="silent <- 1")
    return client.finish()


def test_evaluates_source_without_final_newline(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        answer <- 40
        answer + 2
        """).removesuffix("\n")
    assert not r.endswith("\n")
    client.call_tool("send", r=r)
    assert last_tool_text(client) == "[1] 42\n"
    return client.finish()


def test_recoverable_language_errors(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    client.call_tool("send", r="answer <- 41")
    # fmt: r
    r = code(r"""
        g <- function() stop("boom")
        f <- function() g()
        f()
        """)
    client.call_tool("send", r=r)
    # fmt: r
    r = code(r"""
        traceback()
        """)
    client.call_tool("send", r=r)
    # Trigger an error after evaluation while R auto-prints the visible result.
    # fmt: r
    r = code(r"""
        print.auto_print_failure <- function(...) {
          stop("print failed")
        }
        structure(1, class = "auto_print_failure")
        """)
    client.call_tool("send", r=r)
    client.call_tool("send", r="answer")
    return client.finish()


def test_restarts_after_r_worker_segfault(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    client.call_tool("send", r="r_worker_marker <- TRUE")

    # Ask R's fatal-signal handler to abort after reporting the crash.
    # fmt: r
    r = code(r"""
        tools::pskill(Sys.getpid(), signal = 11L)
        """)
    client.call_tool("send", r=r, stdin="1\n")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    fatal_output = result["content"][0]["text"]
    assert "Possible actions:\n1: abort (with core dump, if enabled)\n" in fatal_output
    assert fatal_output.endswith(
        "Selection: R is aborting now ...\n"
        "worker sideband read failed: worker sideband closed"
    )

    client.call_tool("send", r='exists("r_worker_marker", inherits = FALSE)')
    assert last_tool_text(client) == (
        "[1] FALSE\n\n[worker restarted: in-memory state lost]"
    )
    client.call_tool("send", r="1 + 1")
    assert last_tool_text(client) == "[1] 2\n"
    return client.finish()


def test_reports_r_worker_restart_with_idle_stdin(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    client.call_tool("send", r="invisible(NULL)")

    # fmt: r
    r = code(r"""
        tools::pskill(Sys.getpid(), signal = 9L)
        """).removesuffix("\n")
    client.call_tool("send", r=r)
    assert client.transcript[-1]["result"]["isError"] is True

    client.call_tool("send", stdin="replacement\n")
    assert last_tool_text(client) == (
        "\n[worker restarted: in-memory state lost]\n[idle]"
    )

    # fmt: r
    direct_stdin = code(r"""
        local({
          connection <- suppressWarnings(file("/dev/stdin"))
          on.exit(close(connection))
          readLines(connection, n = 1)
        })
        """)
    client.call_tool("send", r=direct_stdin)
    assert last_tool_text(client) == '[1] "replacement"\n'
    return client.finish()


def test_browser_input(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    client.call_tool("send", r="browser()")
    assert last_tool_text(client).endswith("\n[input]")
    client.call_tool("send", r="1")
    assert client.transcript[-1]["result"]["isError"] is True
    client.call_tool("send", stdin="1 + 1\n")
    assert last_tool_text(client).endswith("\n[input]")
    client.call_tool("send", stdin="c\n")
    assert last_tool_text(client) == "[done]"
    return client.finish()


def test_times_out_and_polls_running_evaluation(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    client.call_tool("send", r="invisible(NULL)")
    # fmt: r
    r = code(r"""
        Sys.sleep(0.25)
        answer <- 42
        answer
        """)
    client.call_tool("send", r=r, timeout_ms=10)
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "\n[running]", output
    client.call_tool("send", timeout_ms=3_000)
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "[1] 42\n", output
    client.call_tool("send", r="answer + 1")
    return client.finish()


def test_routes_idle_and_timed_out_stdin(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()

    # fmt: r
    direct_stdin = code(r"""
        local({
          connection <- suppressWarnings(file("/dev/stdin"))
          on.exit(close(connection))
          readLines(connection, n = 1)
        })
        """)

    client.call_tool("send", stdin="cold fd 0\n")
    assert last_tool_text(client) == "\n[idle]"
    client.call_tool("send", r=direct_stdin)
    assert last_tool_text(client) == '[1] "cold fd 0"\n'

    # fmt: r
    r = code(r"""
        prompted <- readline("bundled> ")
        direct <- local({
          connection <- suppressWarnings(file("/dev/stdin"))
          on.exit(close(connection))
          readLines(connection, n = 1)
        })
        paste(prompted, direct, sep = "|")
        """)
    client.call_tool("send", r=r, stdin="café\n", timeout_ms=50)
    assert last_tool_text(client) == "\n[running]"
    client.call_tool("send", timeout_ms=0)
    assert last_tool_text(client) == "\n[running]"
    client.call_tool("send", stdin="timed out ", timeout_ms=50)
    assert last_tool_text(client) == "\n[running]"
    client.call_tool("send", stdin="fd 0\n", timeout_ms=3_000)
    assert last_tool_text(client) == 'bundled> [1] "café|timed out fd 0"\n'
    return client.finish()


def test_routes_combined_and_followup_stdin(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()

    # fmt: r
    r = code(r"""
        first <- readline("first> ")
        second <- readline("second> ")
        cat(paste(first, second, sep = "|"), "\n", sep = "")
        """)
    client.call_tool("send", r=r, stdin="Ada\nLovelace\n")
    output = last_tool_text(client)
    assert output == "first> second> Ada|Lovelace\n", output

    # fmt: r
    r = code(r"""
        direct <- local({
          connection <- suppressWarnings(file("/dev/stdin"))
          on.exit(close(connection))
          readLines(connection, n = 1)
        })
        prompted <- readline("after> ")
        cat(paste(direct, prompted, sep = "|"), "\n", sep = "")
        """)
    client.call_tool("send", r=r, stdin="direct\n", timeout_ms=1_000)
    output = last_tool_text(client)
    assert output == "after> \n[input]", output
    client.call_tool("send", stdin="callback\n")
    assert last_tool_text(client) == "direct|callback\n"

    # fmt: r
    r = code(r"""
        paste("color", readline("color> "))
        """)
    client.call_tool("send", r=r)
    assert last_tool_text(client) == "color> \n[input]"
    client.call_tool("send", stdin="bl", timeout_ms=50)
    assert last_tool_text(client) == "\n[input]"
    client.call_tool("send", stdin="ue\n")
    assert last_tool_text(client) == '[1] "color blue"\n'

    client.call_tool(
        "send",
        r='invisible(readline("silent> "))',
        stdin="accepted\n",
    )
    assert last_tool_text(client) == "silent> "
    return client.finish()


def test_preserves_fd0_order_between_readers(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()

    # fmt: r
    r = code(r"""
        prompted <- readline("callback> ")
        direct <- local({
          connection <- suppressWarnings(file("/dev/stdin"))
          on.exit(close(connection))
          readLines(connection, n = 1)
        })
        cat(paste(prompted, direct, sep = "|"), "\n", sep = "")
        """)
    client.call_tool(
        "send",
        r=r,
        stdin="callback\ndirect\n",
        timeout_ms=1_000,
    )
    output = last_tool_text(client)
    assert output == "callback> callback|direct\n", output
    return client.finish()


def test_preserves_utf8_across_console_reads(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()

    # fmt: r
    r = code(r"""
        value <- readline("long> ")
        cat(paste(nchar(value, type = "bytes"), endsWith(value, "é")), "\n", sep = "")
        """)
    client.call_tool("send", r=r, stdin=("x" * 4_094) + "é\n")
    client.transcript[-1]["send"]["stdin"] = "<long stdin ending in UTF-8>"
    output = last_tool_text(client)
    assert output == "long> long> 4096 TRUE\n", output
    return client.finish()


def test_keeps_stdin_open_after_partial_payload(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()

    # fmt: r
    r = code(r"""
        cat("before\n")
        value <- readline("partial> ")
        value
        """)
    client.call_tool("send", r=r, stdin="without newline", timeout_ms=1_000)
    output = last_tool_text(client)
    assert output == "before\npartial> \n[input]", output

    client.call_tool("send", stdin="\n")
    assert last_tool_text(client) == '[1] "without newline"\n'

    # fmt: r
    r = code(r"""
        readline("next> ")
        """)
    client.call_tool("send", r=r, stdin="next\n")
    assert last_tool_text(client) == 'next> [1] "next"\n'
    return client.finish()


def last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    return result["content"][0]["text"]


def test_applies_complete_expressions_before_incomplete_source(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        answer <- 42
        answer + (
        """)
    client.call_tool("send", r=r)
    client.call_tool("send", r="answer")
    # fmt: r
    r = code(r"""
        answer <- 43
        )
        """)
    client.call_tool("send", r=r)
    client.call_tool("send", r="answer")
    return client.finish()


def test_runs_native_top_level_bookkeeping(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
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
        mcp_console_callback_probe <- 42
        """)
    client.call_tool("send", r=r)
    # fmt: r
    r = code(r"""
        warning("careful", call. = FALSE)
        invisible(42)
        cat("last value: ", identical(base::.Last.value, 42), "\n", sep = "")
        """)
    client.call_tool("send", r=r)
    return client.finish()


def test_preserves_native_stack_and_last_value_binding(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
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
    client.call_tool("send", r=r)
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
