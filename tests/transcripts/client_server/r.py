#!/usr/bin/env -S uv run --script

import re
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import (
    McpClient,
    Transcript,
    assert_result_content,
    build_r_input_handler,
    code,
    r_test_environment,
    reference_plots,
    release_worker_callback_gate,
    run_this_suite,
    stop_client,
    wait_for_evaluation_output,
    wait_for_idle_output,
    wait_for_worker_file,
)

PLATFORMS = {"darwin", "linux"}


@contextmanager
def r_input_handler_client(binary: Path) -> Iterator[tuple[McpClient, Path]]:
    with tempfile.TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        environment, rscript = r_test_environment()
        environment["TMPDIR"] = temporary_directory
        build_r_input_handler(directory, environment, rscript)
        client = McpClient(
            binary,
            ("serve",),
            environment=environment,
            current_directory=directory,
        )
        try:
            yield client, directory
        finally:
            stop_client(client)


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


def test_services_r_input_handlers_at_cell_boundaries(binary: Path) -> Transcript:
    with r_input_handler_client(binary) as (client, directory):
        client._initialize_and_list_tools()

        # Make the handler ready before the worker's final boundary turn.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            callback_fifo <- file.path(tempdir(), "cell-end-handler-fifo")
            invisible(.Call(
              "mcp_test_register_input_handler",
              callback_fifo,
              function() cat("cell end callback\n")
            ))
            writer <- fifo(callback_fifo, open = "wb")
            writeBin(as.raw(1), writer)
            close(writer)
            """)
        client.send(r=r)
        assert last_tool_text(client) == "cell end callback\n"

        # Register an input handler while its FIFO is empty, then make the
        # descriptor readable before submitting the next cell. Whether the
        # handler runs while idle or during the initial boundary turn, the
        # submitted source must observe its state change.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            cell_start_callback_ran <- FALSE
            invisible(.Call(
              "mcp_test_register_input_handler",
              file.path(tempdir(), "cell-start-handler-fifo"),
              function() cell_start_callback_ran <<- TRUE
            ))
            """)
        client.send(r=r)
        output = last_tool_text(client)
        assert output == "[done]", repr(output)
        fifo = wait_for_worker_file(
            directory,
            "cell-start-handler-fifo",
            client,
        )
        fifo.write_bytes(b"x")
        client.send(r='cat(cell_start_callback_ran, "\\ncell body\\n", sep = "")')
        assert last_tool_text(client) == "TRUE\ncell body\n"
        return client._finish()


def test_services_later_callbacks_while_idle(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          idle_value <<- 42
          cat("idle callback\n")
          stopifnot(file.create(callback_checkpoint))
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, sep = "\n")
        """)
    client.send(r=r)
    release_worker_callback_gate(client, "idle callback")
    client.send(r="idle_value")
    assert last_tool_text(client) == (
        "idle callback\n[output produced while idle]\n[1] 42\n"
    )
    return client._finish()


def test_collects_idle_later_callbacks_with_empty_send(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})
    client.send()
    assert last_tool_text(client) == "\n[idle]"

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          collected_value <<- 84
          cat("collected callback")
          stopifnot(file.create(callback_checkpoint))
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, sep = "\n")
        """)
    client.send(r=r)
    release_worker_callback_gate(client, "collected callback")
    wait_for_idle_output(
        client,
        "collected callback\n[idle]",
        "collected callback output",
    )
    client.send(r="collected_value")
    assert last_tool_text(client) == "[1] 84\n"
    return client._finish()


def test_snapshots_output_while_idle_later_callback_runs(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        callback_release <- tempfile("mcp-console-callback-release-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          stopifnot(file.create(callback_checkpoint))
          while (!file.exists(callback_release)) {
            Sys.sleep(0.01)
          }
          cat("long callback")
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, callback_release, sep = "\n")
        """)
    client.send(r=r)
    (callback_release,) = release_worker_callback_gate(
        client,
        "long idle callback",
        ("release",),
    )
    client.send(timeout_ms=10)
    assert last_tool_text(client) == "\n[idle]"
    callback_release.touch()
    deadline = time.monotonic() + 3
    poll_start = len(client.transcript)
    while True:
        client.send()
        if last_tool_text(client) == "long callback\n[idle]":
            break
        assert last_tool_text(client) == "\n[idle]"
        if time.monotonic() >= deadline:
            raise AssertionError("idle callback output was not collected")
    polls = client.transcript[poll_start:]
    final_poll = polls[-1]
    client.transcript[poll_start:] = [final_poll]
    return client._finish()


def test_restarts_while_idle_callback_runs(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          cat("callback started")
          stopifnot(file.create(callback_checkpoint))
          repeat {
            Sys.sleep(1)
          }
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, sep = "\n")
        """)
    client.send(r=r)
    release_worker_callback_gate(client, "restarted idle callback")
    wait_for_idle_output(
        client,
        "callback started\n[idle]",
        "idle callback output",
        timeout_ms=10,
    )
    client.send(control="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )
    return client._finish()


def test_returns_plots_from_idle_later_callbacks(binary: Path) -> Transcript:
    environment, rscript = r_test_environment()
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          cat("idle plot\n")
          plot(1:3)
          stopifnot(file.create(callback_checkpoint))
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, sep = "\n")
        """)
    client.send(r=r)
    release_worker_callback_gate(client, "idle plot callback")
    expected_plot = reference_plots(
        rscript,
        environment,
        "plot(1:3)",
        width=800 / 96,
        height=600 / 96,
        dpi=96,
        pages=1,
    )
    client.send()
    assert_result_content(
        client,
        ["idle plot\n", expected_plot[0], "\n[idle]"],
    )
    return client._finish()


def test_stops_cell_after_boundary_callback_failure(binary: Path) -> Transcript:
    with r_input_handler_client(binary) as (client, directory):
        client._initialize_and_list_tools()

        # Create the device before removing page permissions. Cairo opens the
        # page at plot time, while Quartz opens it when the device closes.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            invisible(.Call(
              "mcp_test_register_input_handler",
              file.path(tempdir(), "failing-handler-fifo"),
              function() {
                grDevices::dev.new()
                old_umask <- Sys.umask("0777")
                on.exit(Sys.umask(old_umask), add = TRUE)
                plot(1)
                grDevices::dev.off()
                Sys.umask(old_umask)
                on.exit(NULL)
              }
            ))
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"
        fifo = wait_for_worker_file(directory, "failing-handler-fifo", client)
        fifo.write_bytes(b"x")

        client.send(r='system("printf boundary-cell-ran")')
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        output = result["content"][0]["text"]
        assert "boundary-cell-ran" not in output, output
        assert "failed to read managed plot" in output, output
        assert output.endswith(
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        ), output
        result["content"][0]["text"] = (
            "[failed to read managed plot `<worker plot>`: permission denied]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )
        return client._finish()


def test_skips_final_boundary_callbacks_after_cell_failure(binary: Path) -> Transcript:
    with r_input_handler_client(binary) as (client, _directory):
        client._initialize_and_list_tools()

        # Record a plot publication failure during the cell after making an
        # input handler ready for the final boundary turn. The handler writes
        # directly to stdout so its execution would remain observable after
        # sideband publication fails.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            callback_fifo <- file.path(tempdir(), "skipped-handler-fifo")
            invisible(.Call(
              "mcp_test_register_input_handler",
              callback_fifo,
              function() system("printf final-boundary-callback-ran")
            ))
            writer <- fifo(callback_fifo, open = "wb")
            writeBin(as.raw(1), writer)
            close(writer)
            grDevices::dev.new()
            old_umask <- Sys.umask("0777")
            on.exit(Sys.umask(old_umask), add = TRUE)
            plot(1)
            grDevices::dev.off()
            Sys.umask(old_umask)
            on.exit(NULL)
            "cell completed"
            """)
        client.send(r=r)
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        output = result["content"][0]["text"]
        assert "final-boundary-callback-ran" not in output, output
        assert "failed to read managed plot" in output, output
        assert output.endswith(
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        ), output
        result["content"][0]["text"] = (
            "[failed to read managed plot `<worker plot>`: permission denied]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )
        return client._finish()


def test_routes_input_to_idle_later_callbacks_before_a_cell(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          stopifnot(file.create(callback_checkpoint))
          idle_answer <<- readline("later> ")
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, sep = "\n")
        """)
    client.send(r=r)
    release_worker_callback_gate(client, "idle input callback")
    client.send(
        r='cat("cell: ", idle_answer, "\\n", sep = "")',
        stdin="yes\n",
    )
    assert last_tool_text(client) == (
        '[input requested: "later> "]\n[output produced while idle]\ncell: yes\n'
    ), repr(last_tool_text(client))
    return client._finish()


def test_routes_input_to_idle_later_callback(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          stopifnot(file.create(callback_checkpoint))
          collected_answer <<- readline("later> ")
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, sep = "\n")
        """)
    client.send(r=r)
    release_worker_callback_gate(client, "collected input callback")
    wait_for_idle_output(
        client,
        '[input requested: "later> "]\n[waiting for stdin]',
        "idle callback input request",
    )
    poll_start = len(client.transcript)
    client.send(stdin="yes\n")
    deadline = time.monotonic() + 3
    while last_tool_text(client) != "\n[idle]":
        assert last_tool_text(client) == "\n[waiting for stdin]"
        if time.monotonic() >= deadline:
            raise AssertionError("idle callback did not receive submitted stdin")
        time.sleep(0.01)
        client.send()
    polls = client.transcript[poll_start:]
    final_poll = polls[-1]
    final_poll["send"] = polls[0]["send"]
    client.transcript[poll_start:] = [final_poll]
    client.send(r="collected_answer")
    assert last_tool_text(client) == '[1] "yes"\n'
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
    output = result["content"][0]["text"]
    assert "plot.new has not been called yet" in output

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
    assert_result_content(client, [text_items[0]["text"], expected_plot[0]])

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
        "[worker terminated by signal 11]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    ), repr(fatal_output)
    crash_line = next(
        line for line in fatal_output.splitlines() if line.startswith("address ")
    )
    assert re.fullmatch(r"address 0x[0-9a-f]+, cause '[^']+'", crash_line), crash_line
    result["content"][0]["text"] = fatal_output.replace(
        crash_line,
        "address <signal address>, cause '<signal cause>'",
    )

    client.send(r='exists("r_worker_marker", inherits = FALSE)')
    assert last_tool_text(client) == "[1] FALSE\n"
    client.send(r="1 + 1")
    assert last_tool_text(client) == "[1] 2\n"
    return client._finish()


def test_reports_r_worker_exit_status(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    # fmt: r
    r = code(r"""
        quit(save = "no", status = 33L, runLast = FALSE)
        """)
    client.send(r=r)
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker exited with status 33]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    )

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
        "[worker terminated by signal 9]\n"
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


def test_restarts_and_evaluates_r_cell_in_one_send(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(r="inline_restart_marker <- TRUE")

    client.send(
        control="restart",
        r='exists("inline_restart_marker", inherits = FALSE)',
    )
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n"
        "[1] FALSE\n[done]"
    )
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
    assert last_tool_text(client) == (
        '[input requested: "restart> "]\n[waiting for stdin]'
    )

    client.send(control="restart")
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


def test_restart_skips_cell_boundary_callbacks(binary: Path) -> Transcript:
    with r_input_handler_client(binary) as (client, directory):
        client._initialize_and_list_tools()

        # Leave a callback ready for the initial boundary turn. Restart after
        # it requests input, and verify that the submitted cell is never
        # dispatched.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            invisible(.Call(
              "mcp_test_register_input_handler",
              file.path(tempdir(), "initial-boundary-fifo"),
              function() readline("callback> ")
            ))
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"
        fifo = wait_for_worker_file(directory, "initial-boundary-fifo", client)
        fifo.write_bytes(b"x")
        client.send(r='cat("cell body ran\\n")')
        assert last_tool_text(client) == (
            '[input requested: "callback> "]\n[waiting for stdin]'
        )
        client.send(control="restart")
        assert "cell body ran" not in last_tool_text(client)

        # Make the next callback ready before the cell blocks, then restart
        # without letting the worker run its final boundary turn.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            callback_fifo <- file.path(tempdir(), "final-boundary-fifo")
            invisible(.Call(
              "mcp_test_register_input_handler",
              callback_fifo,
              function() cat("post-cell callback ran\n")
            ))
            writer <- fifo(callback_fifo, open = "wb")
            writeBin(as.raw(1), writer)
            close(writer)
            readline("cell> ")
            """)
        client.send(r=r)
        assert last_tool_text(client) == (
            '[input requested: "cell> "]\n[waiting for stdin]'
        )
        client.send(control="restart")
        assert "post-cell callback ran" not in last_tool_text(client)
        return client._finish()


def test_restart_skips_direct_stdin_boundary_callback(binary: Path) -> Transcript:
    with r_input_handler_client(binary) as (client, directory):
        client._initialize_and_list_tools()

        # A direct fd-0 read bypasses the worker's ReadConsole callback.
        # Restart still must prevent the submitted cell from running after EOF
        # releases it.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            invisible(.Call(
              "mcp_test_register_input_handler",
              file.path(tempdir(), "direct-stdin-boundary-fifo"),
              function() {
                connection <- suppressWarnings(file("/dev/stdin"))
                on.exit(close(connection))
                stopifnot(file.create(file.path(
                  tempdir(),
                  "direct-stdin-boundary-checkpoint"
                )))
                readLines(connection, n = 1)
                cat("direct callback released\n")
              }
            ))
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"
        fifo = wait_for_worker_file(
            directory,
            "direct-stdin-boundary-fifo",
            client,
        )
        fifo.write_bytes(b"x")

        waiting = client._start_send(
            r='cat("direct stdin cell ran\\n")',
            timeout_ms=30_000,
        )
        wait_for_worker_file(
            directory,
            "direct-stdin-boundary-checkpoint",
            client,
        )

        restarted = client._start_send(control="restart")
        client._receive(waiting)
        client._receive(restarted)
        assert "direct callback released" in waiting["result"]["content"][0]["text"]
        assert "direct stdin cell ran" not in waiting["result"]["content"][0]["text"]
        assert "direct stdin cell ran" not in restarted["result"]["content"][0]["text"]
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
    client.send(r=r, stdin="n\nn\nn\n")
    output = last_tool_text(client)
    assert output.count('[input requested: "Browse[1]> "]') == 4, output
    assert output.endswith("\n[waiting for stdin]"), output
    assert "n" not in output.splitlines(), output
    client.send(r="1")
    assert client.transcript[-1]["result"]["isError"] is True
    wait_for_evaluation_output(
        client,
        "[1] 3\n",
        "R browser input",
        stdin="c\n",
    )
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
    assert output == "\n[running; poll with an empty send]", output
    client.send(timeout_ms=3_000)
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "[1] 42\n", output
    client.send(r="answer + 1")
    return client._finish()


def test_interrupts_running_r_evaluation(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment, rscript = r_test_environment()
        environment["TMPDIR"] = temporary_directory
        build_r_input_handler(temporary_path, environment, rscript)
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
            current_directory=temporary_path,
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
                repeat {
                  Sys.sleep(60)
                }
                """)
            client.send(r=r, timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            wait_for_worker_file(
                temporary_path,
                "r-interrupt-started",
                client,
            )

            client.send(
                control="interrupt",
                r="interrupt_state + 1L",
                timeout_ms=3_000,
            )
            assert last_tool_text(client) == "\n[1] 42\n[done]"

            # fmt: r
            r = code(r"""
                dyn.load("./mcp_test_input_handler.so")
                invisible(.Call(
                  "mcp_test_register_input_handler",
                  file.path(tempdir(), "input-handler-fifo"),
                  function() {
                    invisible(file.create(file.path(
                      tempdir(),
                      "r-boundary-interrupt-started"
                    )))
                    on.exit(boundary_interrupt_cleanup <<- TRUE)
                    repeat {
                      Sys.sleep(60)
                    }
                  }
                ))
                boundary_interrupt_state <- 42L
                writer <- fifo(
                  file.path(tempdir(), "input-handler-fifo"),
                  open = "wb"
                )
                writeBin(as.raw(1), writer)
                close(writer)
                """)
            client.send(r=r, timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            wait_for_worker_file(
                temporary_path,
                "r-boundary-interrupt-started",
                client,
            )

            client.send(control="interrupt", timeout_ms=0)
            output = last_tool_text(client)
            assert output == "\n", repr(output)
            client.send(r="c(boundary_interrupt_state, boundary_interrupt_cleanup)")
            assert last_tool_text(client) == "[1] 42  1\n"

            client.send(control="interrupt", timeout_ms=0)
            assert last_tool_text(client) == "\n\n[idle]"
            # The controlled send reports idle state after the interrupt
            # grace. The following calls verify that later source still runs.
            client.send(r="idle_interrupt_state <- 42L")
            assert last_tool_text(client) == "[done]"
            client.send(r="idle_interrupt_state")
            assert last_tool_text(client) == "[1] 42\n"

            # Boundary checks must not process elapsed-time limits when no
            # interrupt is pending; R resets the limit when the cell begins.
            # fmt: r
            r = code(r"""
                setTimeLimit(elapsed = 10, transient = FALSE)
                invisible(NULL)
                setTimeLimit(elapsed = 0.05, transient = FALSE)
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]"
            time.sleep(0.1)
            client.send(r='"idle time does not consume a cell limit"')
            assert last_tool_text(client) == (
                '[1] "idle time does not consume a cell limit"\n'
            )
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)


def test_interrupts_managed_console_input(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    passed = False
    try:
        client._initialize_and_list_tools()

        # fmt: r
        r = code(r"""
            r_input_state <- 41L
            tryCatch(
              readline("R interrupt> "),
              interrupt = function(condition) cat("R input interrupted\n")
            )
            """)
        client.send(r=r, stdin="R partial")
        assert last_tool_text(client) == (
            '[input requested: "R interrupt> "]\n[waiting for stdin]'
        )
        client.send(control="interrupt", timeout_ms=0)
        assert last_tool_text(client) == "R input interrupted\n"
        client.send(r='readline("R replay> ")', stdin="!\n")
        assert last_tool_text(client) == (
            '[input requested: "R replay> "]\n[1] "R partial!"\n'
        )

        # fmt: r
        r = code(r"""
            suspended_input <- "not read"
            suspendInterrupts({
              suspended_input <- readline("R suspended> ")
              cat("suspended input accepted")
            })
            """)
        client.send(r=r)
        assert last_tool_text(client) == (
            '[input requested: "R suspended> "]\n[waiting for stdin]'
        )
        client.send(control="interrupt", timeout_ms=0)
        assert last_tool_text(client) == "\n[waiting for stdin]"
        wait_for_evaluation_output(
            client,
            "suspended input accepted\n",
            "suspended R console input",
            stdin="accepted\n",
            timeout_ms=3_000,
        )
        client.send(r="suspended_input")
        assert last_tool_text(client) == '[1] "accepted"\n'

        # fmt: python
        python = code("""
            python_input_state = 41
            try:
                input("Python interrupt> ")
            except KeyboardInterrupt:
                print("Python input interrupted")
            """)
        client.send(python=python, stdin="Python partial")
        assert last_tool_text(client) == (
            '[input requested: "Python interrupt> "]\n[waiting for stdin]'
        )
        client.send(control="interrupt", timeout_ms=0)
        assert last_tool_text(client) == "Python input interrupted\n"
        client.send(python='input("Python replay> ")', stdin="!\n")
        assert last_tool_text(client) == (
            "[input requested: \"Python replay> \"]\n'Python partial!'\n"
        )

        client.send(r="r_input_state + 1L")
        assert last_tool_text(client) == "[1] 42\n"
        client.send(python="python_input_state + 1")
        assert last_tool_text(client) == "42\n"
        transcript = client._finish()
        passed = True
        return transcript
    finally:
        if not passed:
            stop_client(client)


def test_replays_console_prefix_after_operation_boundary_interrupt(
    binary: Path,
) -> Transcript:
    with r_input_handler_client(binary) as (client, directory):
        client._initialize_and_list_tools()

        # A small native buffer makes a later managed callback deterministic
        # without relying on the server's 10 millisecond request grace.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            tryCatch(
              .Call(
                "mcp_test_read_console_line",
                "small callbacks> "
              ),
              interrupt = function(condition) {
                cat("caught later-callback interrupt\n")
              }
            )
            """)
        client.send(r=r, stdin="part")
        assert last_tool_text(client) == (
            '[input requested: "small callbacks> "]\n'
            '[input requested: "small callbacks> "]\n'
            "[waiting for stdin]"
        )

        client.send(control="interrupt", timeout_ms=0)
        assert last_tool_text(client) == "caught later-callback interrupt\n"

        client.send(r='readline("after later callback> ")', stdin="!\n")
        assert last_tool_text(client) == (
            '[input requested: "after later callback> "]\n[1] "part!"\n'
        )

        # Return one full console buffer, then interrupt before another
        # callback can complete the logical line.
        # fmt: r
        r = code(r"""
            tryCatch(
              {
                partial <- .Call(
                  "mcp_test_read_console_once",
                  "between callbacks> "
                )
                stopifnot(nchar(partial, type = "bytes") == 3L)
                invisible(file.create(file.path(
                  tempdir(),
                  "between-console-callbacks-started"
                )))
                repeat {
                  Sys.sleep(60)
                }
              },
              interrupt = function(condition) {
                cat("caught between-callback interrupt\n")
              }
            )
            """)
        client.send(r=r)
        assert last_tool_text(client) == (
            '[input requested: "between callbacks> "]\n[waiting for stdin]'
        )
        client.send(stdin="x" * 6, timeout_ms=50)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        wait_for_worker_file(
            directory,
            "between-console-callbacks-started",
            client,
        )

        client.send(control="interrupt", timeout_ms=0)
        assert last_tool_text(client) == "caught between-callback interrupt\n"

        client.send(
            r='identical(readline("after boundary> "), paste0(strrep("x", 6), "!"))',
            stdin="!\n",
        )
        output = last_tool_text(client)
        assert output == ('[input requested: "after boundary> "]\n[1] TRUE\n'), repr(
            output
        )
        return client._finish()


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
        "\n[running; poll with an empty send]",
        '[input requested: "bundled> "]\n\n[running; poll with an empty send]',
    }, first_output
    client.transcript[-1]["result"]["content"][0]["text"] = (
        "\n[running; poll with an empty send]"
    )
    client.send(timeout_ms=0)
    assert last_tool_text(client) == "\n[running; poll with an empty send]"
    client.send(stdin="timed out ", timeout_ms=50)
    assert last_tool_text(client) == "\n[running; poll with an empty send]"
    client.send(stdin="fd 0\n", timeout_ms=3_000)
    final_output = last_tool_text(client)
    expected_result = '[1] "café|timed out fd 0"\n'
    if first_output == "\n[running; poll with an empty send]":
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
    assert output == '[input requested: "after> "]\n[waiting for stdin]', output
    client.send(stdin="callback\n")
    assert last_tool_text(client) == "direct|callback\n"

    # fmt: r
    r = code(r"""
        paste("color", readline("color> "))
        """)
    client.send(r=r)
    assert last_tool_text(client) == '[input requested: "color> "]\n[waiting for stdin]'
    client.send(stdin="bl", timeout_ms=50)
    assert last_tool_text(client) == "\n[waiting for stdin]"
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
    expected = '[input requested: "callback> "]\ncallback|direct\n'
    call_start = len(client.transcript)
    client.send(
        r=r,
        stdin="callback\ndirect\n",
        timeout_ms=1_000,
    )
    deadline = time.monotonic() + 3
    while last_tool_text(client) != expected:
        output = last_tool_text(client)
        assert output == "\n[running; poll with an empty send]", output
        if time.monotonic() >= deadline:
            raise AssertionError("ordered fd 0 readers did not finish")
        client.send(timeout_ms=1_000)

    calls = client.transcript[call_start:]
    final_call = calls[-1]
    final_call["send"] = calls[0]["send"]
    client.transcript[call_start:] = [final_call]
    return client._finish()


def test_preserves_utf8_across_console_reads(binary: Path) -> Transcript:
    with r_input_handler_client(binary) as (client, _):
        client._initialize_and_list_tools()

        # The four-byte native buffer splits the two-byte character across
        # callbacks without making thousands of single-byte reads.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            first <- .Call("mcp_test_read_console_once", "short> ")
            second <- .Call("mcp_test_read_console_once", "short> ")
            bytes <- c(charToRaw(first), charToRaw(second))
            value <- rawToChar(bytes[bytes != as.raw(10)])
            Encoding(value) <- "UTF-8"
            cat(
              paste(nchar(value, type = "bytes"), endsWith(value, "é")),
              "\n",
              sep = ""
            )
            """)
        client.send(r=r)
        assert last_tool_text(client) == (
            '[input requested: "short> "]\n[waiting for stdin]'
        )

        client.send(stdin="xx")
        assert last_tool_text(client) == "\n[waiting for stdin]"

        client.send(stdin="é")
        assert last_tool_text(client) == (
            '[input requested: "short> "]\n[waiting for stdin]'
        )

        wait_for_evaluation_output(
            client,
            "4 TRUE\n",
            "UTF-8 console input",
            stdin="\n",
            timeout_ms=3_000,
        )
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
    client.send(r=r, stdin="without newline", timeout_ms=0)
    assert last_tool_text(client) == "\n[running; poll with an empty send]"

    client.send(timeout_ms=3_000)
    output = last_tool_text(client)
    assert output == 'before\n[input requested: "partial> "]\n[waiting for stdin]', (
        output
    )

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
