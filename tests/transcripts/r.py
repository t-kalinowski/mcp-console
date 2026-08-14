#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from pathlib import Path

from _support import (
    McpClient,
    Transcript,
    assert_result_content,
    code,
    r_test_environment,
    reference_plots,
    run_this_suite,
    stop_client,
    wait_for_worker_file,
)

PLATFORMS = {"darwin"}


def test_evaluates_a_complete_cell(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        answer <- 40
        answer + 1
        answer + 2
        cat("done\n")
        invisible(99)
        """)
    client.send(r=r)

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
    client.send(r=r)
    client.send(r='stop("boom")')
    client.send(r="answer")
    client.send(r="silent <- 1")
    return client._finish()


def test_uses_200_column_default(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        cat("width: ", getOption("width"), "\n", sep = "")
        1:45
        """)
    client.send(r=r)
    output = last_tool_text(client)
    lines = output.splitlines()
    assert lines[0] == "width: 200", repr(output)
    assert len(lines) == 2, repr(output)
    assert lines[1].startswith(" [1]"), repr(output)
    assert lines[1].endswith(" 45"), repr(output)
    return client._finish()


def test_returns_cell_scoped_plots(binary: Path) -> Transcript:
    environment, rscript = r_test_environment()
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        options(
          console.plot.width = 4,
          console.plot.height = 3,
          console.plot.dpi = 100
        )
        cat("before plots\n")
        local({
          plot(1:3)
          cat("after first plot\n")
          lines(3:1, col = "red")
          plot(3:1)
          cat("after second plot\n")
        })
        """)
    expected_plots = reference_plots(
        rscript,
        environment,
        r,
        width=4,
        height=3,
        dpi=100,
        pages=2,
    )
    client.send(r=r)
    assert_result_content(
        client,
        [
            "before plots\nafter first plot\n",
            expected_plots[0],
            "after second plot\n",
            expected_plots[1],
        ],
    )

    client.send(r="lines(1:3)")
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    assert len(result["content"]) == 1, result
    assert "plot.new has not been called yet" in result["content"][0]["text"]
    result["content"][0]["text"] = "Error: plot.new has not been called yet\n"

    r = "plot(3:1)"
    expected_plot = reference_plots(
        rscript,
        environment,
        r,
        width=4,
        height=3,
        dpi=100,
        pages=1,
    )
    client.send(r=r)
    assert_result_content(client, expected_plot)
    return client._finish()


def test_emits_managed_plots_when_pages_finalize(binary: Path) -> Transcript:
    environment, rscript = r_test_environment()
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        options(
          console.plot.width = 4,
          console.plot.height = 3,
          console.plot.dpi = 100
        )
        local({
          plot(1:3)
          plot(3:1)
          cat("after first page finalized\n")
        })
        """)
    expected_plots = reference_plots(
        rscript,
        environment,
        r,
        width=4,
        height=3,
        dpi=100,
        pages=2,
    )
    client.send(r=r)
    assert_result_content(
        client,
        [
            expected_plots[0],
            "after first page finalized\n",
            expected_plots[1],
        ],
    )
    return client._finish()


def test_returns_plots_after_r_errors(binary: Path) -> Transcript:
    environment, rscript = r_test_environment()
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        local({
          plot(1:3)
          stop("boom")
        })
        """)
    expected_plot = reference_plots(
        rscript,
        environment,
        r,
        width=800 / 96,
        height=600 / 96,
        dpi=96,
        pages=1,
        expected_error="boom",
    )
    client.send(r=r)
    result = client.transcript[-1]["result"]
    text_items = [item for item in result["content"] if item["type"] == "text"]
    assert len(text_items) == 1 and "boom" in text_items[0]["text"], result
    text_items[0]["text"] = "Error: boom\n"
    assert_result_content(client, ["Error: boom\n", expected_plot[0]])

    r = "plot(3:1)"
    expected_plot = reference_plots(
        rscript,
        environment,
        r,
        width=800 / 96,
        height=600 / 96,
        dpi=96,
        pages=1,
    )
    client.send(r=r)
    assert_result_content(client, expected_plot)
    return client._finish()


def test_leaves_explicit_plot_devices_user_controlled(binary: Path) -> Transcript:
    environment, rscript = r_test_environment()
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        explicit_plot <- tempfile(fileext = ".png")
        grDevices::png(explicit_plot, width = 4, height = 3, units = "in", res = 100)
        explicit_device <- grDevices::dev.cur()
        plot(1:3)
        cat("explicit current: ", names(grDevices::dev.cur()), "\n", sep = "")
        """)
    client.send(r=r)
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    assert result["content"][0]["text"].startswith("explicit current: "), result
    result["content"][0]["text"] = "explicit current: <device>\n"
    assert result["content"] == [
        {"type": "text", "text": "explicit current: <device>\n"}
    ], result

    # fmt: r
    r = code(r"""
        cat(
          "explicit still current: ",
          identical(grDevices::dev.cur(), explicit_device),
          "\n",
          sep = ""
        )
        invisible(grDevices::dev.off(which = explicit_device))
        cat("explicit complete: ", file.exists(explicit_plot), "\n", sep = "")
        unlink(explicit_plot)
        """)
    client.send(r=r)
    result = client.transcript[-1]["result"]
    assert result == {
        "content": [
            {
                "type": "text",
                "text": "explicit still current: TRUE\nexplicit complete: TRUE\n",
            }
        ],
        "isError": False,
    }, result

    r = "plot(3:1)"
    expected_plot = reference_plots(
        rscript,
        environment,
        r,
        width=800 / 96,
        height=600 / 96,
        dpi=96,
        pages=1,
    )
    client.send(r=r)
    assert_result_content(client, expected_plot)

    # fmt: r
    r = code(r"""
        first_explicit_plot <- tempfile(fileext = ".png")
        second_explicit_plot <- tempfile(fileext = ".png")
        grDevices::png(first_explicit_plot)
        plot(1:3)
        grDevices::png(second_explicit_plot)
        plot(3:1)
        grDevices::graphics.off()
        cat(
          "all explicit complete: ",
          all(file.exists(first_explicit_plot, second_explicit_plot)),
          "\n",
          sep = ""
        )
        unlink(c(first_explicit_plot, second_explicit_plot))
        plot(2:4)
        """)
    expected_plot = reference_plots(
        rscript,
        environment,
        r,
        width=800 / 96,
        height=600 / 96,
        dpi=96,
        pages=1,
    )
    client.send(r=r)
    assert_result_content(
        client,
        ["all explicit complete: TRUE\n", expected_plot[0]],
    )
    return client._finish()


def test_evaluates_source_without_final_newline(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        answer <- 40
        answer + 2
        """).removesuffix("\n")
    assert not r.endswith("\n")
    client.send(r=r)
    assert last_tool_text(client) == "[1] 42\n"
    return client._finish()


def test_recoverable_language_errors(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(r="answer <- 41")
    # fmt: r
    r = code(r"""
        g <- function() stop("boom")
        f <- function() g()
        f()
        """)
    client.send(r=r)
    # fmt: r
    r = code(r"""
        traceback()
        """)
    client.send(r=r)
    # Trigger an error after evaluation while R auto-prints the visible result.
    # fmt: r
    r = code(r"""
        print.auto_print_failure <- function(...) {
          stop("print failed")
        }
        structure(1, class = "auto_print_failure")
        """)
    client.send(r=r)
    client.send(r="answer")
    return client._finish()


def test_restarts_after_r_worker_segfault(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(r="r_worker_marker <- TRUE")

    # Ask R's fatal-signal handler to abort after reporting the crash.
    # fmt: r
    r = code(r"""
        tools::pskill(Sys.getpid(), signal = 11L)
        """)
    client.send(r=r, stdin="1\n")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    fatal_output = result["content"][0]["text"]
    assert "Possible actions:\n1: abort (with core dump, if enabled)\n" in fatal_output
    assert fatal_output.endswith(
        '[input requested: "Selection: "]\nR is aborting now ...\n'
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    )

    client.send(r='exists("r_worker_marker", inherits = FALSE)')
    assert last_tool_text(client) == "[1] FALSE\n"
    client.send(r="1 + 1")
    assert last_tool_text(client) == "[1] 2\n"
    return client._finish()


def test_reports_r_worker_restart_with_idle_stdin(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(r="invisible(NULL)")

    # fmt: r
    r = code(r"""
        tools::pskill(Sys.getpid(), signal = 9L)
        """).removesuffix("\n")
    client.send(r=r)
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    )

    client.send(stdin="replacement\n")
    assert last_tool_text(client) == "\n[idle]"

    # fmt: r
    direct_stdin = code(r"""
        local({
          connection <- suppressWarnings(file("/dev/stdin"))
          on.exit(close(connection))
          readLines(connection, n = 1)
        })
        """)
    client.send(r=direct_stdin)
    assert last_tool_text(client) == '[1] "replacement"\n'
    return client._finish()


def test_restart_while_r_waits_for_input(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        restart_marker <- TRUE
        readline("restart> ")
        """)
    client.send(r=r)
    assert last_tool_text(client) == ('[input requested: "restart> "]\n[stdin needed]')

    client.session(action="restart")
    output = last_tool_text(client)
    assert output == (
        '[1] ""\n[active evaluation stopped by session restart request]\n'
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    ), repr(output)

    client.send(r='exists("restart_marker", inherits = FALSE)')
    assert last_tool_text(client) == "[1] FALSE\n"
    return client._finish()


def test_browser_input(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        step <- function() {
          value <- 1
          browser()
          value <- value + 1
          value <- value + 1
          value
        }
        step()
        """)
    client.send(r=r)
    output = last_tool_text(client)
    assert output.count('[input requested: "Browse[1]> "]') == 1, output
    assert output.endswith("\n[stdin needed]"), output
    client.send(r="1")
    assert client.transcript[-1]["result"]["isError"] is True
    client.send(stdin="n\nn\nn\n")
    output = last_tool_text(client)
    assert output.count('[input requested: "Browse[1]> "]') == 3, output
    assert output.endswith("\n[stdin needed]"), output
    assert "n" not in output.splitlines(), output
    client.send(stdin="c\n")
    output = last_tool_text(client)
    assert output == "[1] 3\n", output
    return client._finish()


def test_times_out_and_polls_running_evaluation(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(r="invisible(NULL)")
    # fmt: r
    r = code(r"""
        Sys.sleep(0.25)
        answer <- 42
        answer
        """)
    client.send(r=r, timeout_ms=10)
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "\n[running]", output
    client.send(timeout_ms=3_000)
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "[1] 42\n", output
    client.send(r="answer + 1")
    return client._finish()


def test_interrupts_running_r_evaluation(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        # fmt: python
        launcher = code("""
            import os
            import signal
            import sys

            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
            os.execv(sys.argv[1], sys.argv[1:])
            """)
        client = McpClient(
            Path(sys.executable),
            ("-c", launcher, str(binary), "serve"),
            environment,
        )
        passed = False
        try:
            client._initialize_and_list_tools()
            # fmt: r
            r = code(r"""
                interrupt_state <- 41L
                invisible(file.create(file.path(
                  tempdir(),
                  "r-interrupt-started"
                )))
                repeat {}
                """)
            client.send(r=r, timeout_ms=0)
            assert last_tool_text(client) == "\n[running]"
            wait_for_worker_file(
                temporary_path,
                "r-interrupt-started",
                client,
            )

            client.session(action="interrupt")
            assert last_tool_text(client) == "[interrupt sent]"
            client.send(timeout_ms=3_000)
            assert last_tool_text(client) == "\n"

            client.send(r="interrupt_state + 1L")
            assert last_tool_text(client) == "[1] 42\n"

            client.session(action="interrupt")
            assert last_tool_text(client) == "[interrupt sent]"
            client.send(r="6 * 7")
            assert last_tool_text(client) == "\n[1] 42\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)


def test_routes_idle_and_timed_out_stdin(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    # fmt: r
    direct_stdin = code(r"""
        local({
          connection <- suppressWarnings(file("/dev/stdin"))
          on.exit(close(connection))
          readLines(connection, n = 1)
        })
        """)

    client.send(stdin="cold fd 0\n")
    assert last_tool_text(client) == "\n[idle]"
    client.send(r=direct_stdin)
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
    client.send(r=r, stdin="café\n", timeout_ms=50)
    first_output = last_tool_text(client)
    assert first_output in {
        "\n[running]",
        '[input requested: "bundled> "]\n\n[running]',
    }, first_output
    client.transcript[-1]["result"]["content"][0]["text"] = "\n[running]"
    client.send(timeout_ms=0)
    assert last_tool_text(client) == "\n[running]"
    client.send(stdin="timed out ", timeout_ms=50)
    assert last_tool_text(client) == "\n[running]"
    client.send(stdin="fd 0\n", timeout_ms=3_000)
    final_output = last_tool_text(client)
    expected_result = '[1] "café|timed out fd 0"\n'
    if first_output == "\n[running]":
        expected_result = '[input requested: "bundled> "]\n' + expected_result
    assert final_output == expected_result, final_output
    client.transcript[-1]["result"]["content"][0]["text"] = (
        '[input requested: "bundled> "]\n[1] "café|timed out fd 0"\n'
    )
    return client._finish()


def test_routes_combined_and_followup_stdin(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    # fmt: r
    r = code(r"""
        first <- readline("first> ")
        second <- readline("second> ")
        cat(paste(first, second, sep = "|"), "\n", sep = "")
        """)
    client.send(r=r, stdin="Ada\nLovelace\n")
    output = last_tool_text(client)
    assert output == (
        '[input requested: "first> "]\n[input requested: "second> "]\nAda|Lovelace\n'
    ), output

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
    client.send(r=r, stdin="direct\n", timeout_ms=1_000)
    output = last_tool_text(client)
    assert output == '[input requested: "after> "]\n[stdin needed]', output
    client.send(stdin="callback\n")
    assert last_tool_text(client) == "direct|callback\n"

    # fmt: r
    r = code(r"""
        paste("color", readline("color> "))
        """)
    client.send(r=r)
    assert last_tool_text(client) == '[input requested: "color> "]\n[stdin needed]'
    client.send(stdin="bl", timeout_ms=50)
    assert last_tool_text(client) == "\n[stdin needed]"
    client.send(stdin="ue\n")
    assert last_tool_text(client) == '[1] "color blue"\n'

    # fmt: r
    r = code(r"""
        prompt <- paste0('quoted "prompt"', "\n", "> ")
        invisible(readline(prompt))
        """)
    client.send(r=r, stdin="accepted\n")
    output = last_tool_text(client)
    assert output == '[input requested: "quoted \\"prompt\\"\\n> "]\n', output
    assert "accepted" not in output
    return client._finish()


def test_preserves_fd0_order_between_readers(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

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
    client.send(
        r=r,
        stdin="callback\ndirect\n",
        timeout_ms=1_000,
    )
    output = last_tool_text(client)
    assert output == '[input requested: "callback> "]\ncallback|direct\n', output
    return client._finish()


def test_preserves_utf8_across_console_reads(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    # fmt: r
    r = code(r"""
        value <- readline("long> ")
        cat(paste(nchar(value, type = "bytes"), endsWith(value, "é")), "\n", sep = "")
        """)
    client.send(r=r, stdin=("x" * 4_094) + "é\n")
    client.transcript[-1]["send"]["stdin"] = "<long stdin ending in UTF-8>"
    output = last_tool_text(client)
    assert output == (
        '[input requested: "long> "]\n[input requested: "long> "]\n4096 TRUE\n'
    ), output
    return client._finish()


def test_keeps_stdin_open_after_partial_payload(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    # fmt: r
    r = code(r"""
        cat("before\n")
        value <- readline("partial> ")
        value
        """)
    client.send(r=r, stdin="without newline", timeout_ms=1_000)
    output = last_tool_text(client)
    assert output == 'before\n[input requested: "partial> "]\n[stdin needed]', output

    client.send(stdin="\n")
    assert last_tool_text(client) == '[1] "without newline"\n'

    # fmt: r
    r = code(r"""
        readline("next> ")
        """)
    client.send(r=r, stdin="next\n")
    assert last_tool_text(client) == '[input requested: "next> "]\n[1] "next"\n'
    return client._finish()


def last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    return result["content"][0]["text"]


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
