#!/usr/bin/env -S uv run --script

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import (
    FifoCheckpoint,
    McpClient,
    Transcript,
    collect_running_output,
    code,
    run_this_suite,
    stop_client,
    wait_for_evaluation_output,
)

PLATFORMS = {"darwin"}


from client_server._harness import (
    _r_last_tool_text as last_tool_text,
    r_input_handler_client,
)


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
    # Same-call stdin is transport-ordered, but fd 0 consumption can lag the
    # input-exposure response. Accumulate exact public output cuts in order.
    expected = (
        '[input requested: "first> "]\n[input requested: "second> "]\nAda|Lovelace\n'
    )
    waiting = "[waiting for stdin]"
    deadline = time.monotonic() + 3
    call_start = len(client.transcript)
    result = client.send(r=r, stdin="Ada\nLovelace\n")
    collected = ""
    while True:
        assert result.get("isError") is not True, result
        content = result["content"]
        assert len(content) == 1 and content[0]["type"] == "text", content
        output = content[0]["text"]
        is_waiting = output.endswith(waiting)
        if is_waiting:
            delta = "" if output == "\n" + waiting else output.removesuffix(waiting)
        else:
            delta = output
        collected += delta
        assert expected.startswith(collected), repr(collected)
        if not is_waiting:
            assert collected == expected, repr(collected)
            break
        assert collected, "the first input request was not reported"
        assert time.monotonic() < deadline, "combined same-call stdin did not complete"
        result = client.send(timeout_ms=3_000)

    calls = client.transcript[call_start:]
    submitted = calls[0]
    final_result = calls[-1]["result"]
    final_result["content"][0]["text"] = collected
    submitted["result"] = final_result
    client.transcript[call_start:] = [submitted]

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
    released = False
    finished = False
    checkpoints: list[FifoCheckpoint] = []
    try:
        client._initialize_and_list_tools()

        # Create the FIFOs inside the worker's private writable directory.
        # fmt: r
        r = code(r"""
            fd0_order_started <- tempfile("mcp-console-fd0-order-started-")
            fd0_order_release <- tempfile("mcp-console-fd0-order-release-")
            cat(fd0_order_started, fd0_order_release, sep = "\n")
            """)
        client.send(r=r)
        setup = client.transcript[-1]["result"]
        paths = setup["content"][0]["text"].splitlines()
        assert len(paths) == 2, setup
        setup["content"][0]["text"] = (
            "<fd 0 started checkpoint>\n<fd 0 release checkpoint>"
        )
        started, release = [FifoCheckpoint(Path(path)) for path in paths]
        checkpoints.extend((started, release))

        # Hold the worker after same-call stdin has been queued but before
        # either reader can consume it. The following public poll then observes
        # both reads independent of process scheduling.
        # fmt: r
        r = code(r"""
            started <- fifo(fd0_order_started, open = "wb", blocking = TRUE)
            writeBin(charToRaw("1"), started)
            close(started)
            release <- fifo(fd0_order_release, open = "rb", blocking = TRUE)
            stopifnot(identical(
              readBin(release, "raw", n = 1L),
              charToRaw("1")
            ))
            close(release)

            prompted <- readline("callback> ")
            direct <- local({
              connection <- suppressWarnings(file("/dev/stdin"))
              on.exit(close(connection))
              readLines(connection, n = 1)
            })
            cat(paste(prompted, direct, sep = "|"), "\n", sep = "")
            """)
        evaluation = client._start_send(
            r=r,
            stdin="callback\ndirect\n",
            timeout_ms=0,
        )
        started.wait("ordered fd 0 readers")
        client._receive(evaluation)
        assert evaluation["result"]["content"] == [
            {
                "type": "text",
                "text": "\n[running; poll with an empty send]",
            }
        ], evaluation
        release.release()
        released = True

        client.send()
        expected = '[input requested: "callback> "]\ncallback|direct\n'
        assert last_tool_text(client) == expected
        evaluation["result"] = client.transcript[-1]["result"]
        client.transcript[-2:] = [evaluation]

        transcript = client._finish()
        finished = True
        return transcript
    finally:
        if checkpoints and not released:
            release.release()
        for checkpoint in checkpoints:
            checkpoint.close()
        if not finished:
            stop_client(client)


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

    cuts = collect_running_output(
        client,
        "partial stdin input request",
        timeouts_ms=(3_000,) * 5,
    )
    output = "".join(cuts)
    expected = 'before\n[input requested: "partial> "]\n[waiting for stdin]'
    assert output == expected, repr(output)

    client.send(stdin="\n")
    assert last_tool_text(client) == '[1] "without newline"\n'

    # fmt: r
    r = code(r"""
        readline("next> ")
        """)
    client.send(r=r, stdin="next\n")
    assert last_tool_text(client) == '[input requested: "next> "]\n[1] "next"\n'
    return client._finish()


if __name__ == "__main__":
    run_this_suite(__file__)
