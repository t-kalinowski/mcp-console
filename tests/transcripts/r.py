#!/usr/bin/env -S uv run --script

from pathlib import Path
from textwrap import dedent

from _support import McpClient, Transcript, run_this_suite


PLATFORMS = {"darwin"}


def send_r(client: McpClient, source: str) -> None:
    client.call_tool("send", r=dedent(source).strip("\n"))


def initialized_client(binary: Path) -> McpClient:
    client = McpClient(binary, ("serve",))
    client.initialize()
    return client


def test_top_level_cells(binary: Path) -> Transcript:
    client = initialized_client(binary)
    send_r(
        client,
        # fmt: r
        r"""
        answer <- (38 + 2)
        answer + 2
        cat("done\n")
        invisible(99)
        """,
    )
    send_r(
        client,
        # fmt: r
        r"""
        silent <- 1
        """,
    )
    send_r(
        client,
        # fmt: r
        r"""
        1
        2
        answer
        """,
    )
    send_r(
        client,
        # fmt: r
        r"""
        cores <- parallel::detectCores()
        "parallel" %in% names(getLoadedDLLs())
        """,
    )
    send_r(
        client,
        # fmt: r
        r"""
        ..mcp_console_value.. <- 42
        ..mcp_console_value..
        """,
    )
    send_r(
        client,
        # fmt: r
        r"""
        job <- parallel::mcparallel(cat("forked output\n"))
        invisible(parallel::mccollect(job))
        """,
    )
    return client.finish()


def test_repl_bookkeeping(binary: Path) -> Transcript:
    client = initialized_client(binary)
    send_r(
        client,
        # fmt: r
        r"""
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
        """,
    )
    send_r(
        client,
        # fmt: r
        r"""
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
        """,
    )
    send_r(
        client,
        # fmt: r
        r"""
        warning("careful", call. = FALSE)
        invisible(42)
        cat(
          "last value: ",
          identical(base::.Last.value, 42),
          ", global binding: ",
          exists(".Last.value", envir = globalenv(), inherits = FALSE),
          "\n",
          sep = ""
        )
        """,
    )
    return client.finish()


def test_recoverable_language_errors(binary: Path) -> Transcript:
    client = initialized_client(binary)
    send_r(
        client,
        # fmt: r
        r"""
        answer <- 40
        """,
    )
    send_r(
        client,
        # fmt: r
        r"""
        answer <- 41
        answer + (
        """,
    )
    send_r(
        client,
        # fmt: r
        r"""
        answer
        """,
    )
    send_r(
        client,
        # fmt: r
        r"""
        cat("before\n")
        stop("boom", call. = FALSE)
        """,
    )
    send_r(
        client,
        # fmt: r
        r"""
        answer
        """,
    )
    send_r(
        client,
        # fmt: r
        r"""
        g <- function() stop("boom", call. = FALSE)
        f <- function() g()
        f()
        """,
    )
    send_r(
        client,
        # fmt: r
        r"""
        traceback_lines <- capture.output(traceback())
        cat(
          "traceback: stop=",
          any(grepl("stop", traceback_lines, fixed = TRUE)),
          ", g=",
          any(grepl("g()", traceback_lines, fixed = TRUE)),
          ", f=",
          any(grepl("f()", traceback_lines, fixed = TRUE)),
          ", internal=",
          any(grepl("mcp_console|base::get", traceback_lines)),
          "\n",
          sep = ""
        )
        """,
    )
    send_r(
        client,
        # fmt: r
        r"""
        print.transcript_boom <- function(...) {
          stop("print failed")
        }
        structure(1, class = "transcript_boom")
        """,
    )
    send_r(
        client,
        # fmt: r
        r"""
        answer
        """,
    )
    return client.finish()


def test_readline_input(binary: Path) -> Transcript:
    client = initialized_client(binary)
    send_r(
        client,
        # fmt: r
        r"""
        name <- readline("name> ")
        paste("hello", name)
        """,
    )
    client.call_tool("send", stdin="Ad")
    client.call_tool("send", stdin="a\n")
    client.call_tool("send", stdin="unused\n")
    return client.finish()


def test_buffered_stdin(binary: Path) -> Transcript:
    client = initialized_client(binary)
    send_r(
        client,
        # fmt: r
        r"""
        first <- readline("first> ")
        second <- readline("second> ")
        paste(first, second)
        """,
    )
    client.call_tool("send", stdin="one\ntwo\nunused\n")
    send_r(
        client,
        # fmt: r
        r"""
        fresh <- readline("fresh> ")
        fresh
        """,
    )
    client.call_tool("send", stdin="kept\n")
    send_r(
        client,
        # fmt: r
        r"""
        readline("fail> ")
        stop("boom", call. = FALSE)
        """,
    )
    client.call_tool("send", stdin="used\nstale\n")
    send_r(
        client,
        # fmt: r
        r"""
        fresh <- readline("after error> ")
        fresh
        """,
    )
    client.call_tool("send", stdin="new\n")
    return client.finish()


def test_browser_input(binary: Path) -> Transcript:
    client = initialized_client(binary)
    send_r(
        client,
        # fmt: r
        r"""
        browser()
        """,
    )
    send_r(
        client,
        # fmt: r
        r"""
        1
        """,
    )
    client.call_tool("send", stdin="1 + 1\n")
    client.call_tool("send", stdin="c\n")
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
