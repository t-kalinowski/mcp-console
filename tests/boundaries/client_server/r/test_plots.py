#!/usr/bin/env -S uv run --script

import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import (
    FifoCheckpoint,
    McpClient,
    Transcript,
    assert_result_content,
    build_r_input_handler,
    collect_running_output,
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

PLATFORMS = {"darwin"}


from client_server._harness import (
    _r_last_tool_text as last_tool_text,
)


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


if __name__ == "__main__":
    run_this_suite(__file__)
