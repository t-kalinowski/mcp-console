#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.relay_worker._harness import RelayWorkerClient
from support.assertions import tool_text as _tool_text
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite


PLATFORMS = {"darwin"}


def test_routes_python_output(binary: Path) -> Transcript:
    client = RelayWorkerClient(binary)
    # fmt: r
    r = code(r"""
        suppressWarnings(
          invisible(reticulate::py_run_string("initialized_from_r = True"))
        )
        """)
    assert _tool_text(client.send(r=r)) == "[done]"

    # fmt: python
    python = code(r"""
        import sys

        assert initialized_from_r
        print("Python stdout")
        sys.stderr.write("Python stderr\n")
        raise ValueError("boom")
        """)
    output = _tool_text(client.send(python=python))
    assert output.startswith("Python stdout\nPython stderr\nTraceback"), output
    assert output.endswith("ValueError: boom\n"), output

    # fmt: python
    descendant = code(r"""
        import sys

        print("exec descendant stdout")
        sys.stderr.write("exec descendant stderr\n")
        """)
    # fmt: python
    python = code(rf"""
        import os
        import subprocess
        import sys

        buffer_stdout = sys.stdout.buffer.write(b"buffer stdout\n")
        sys.stdout.buffer.flush()
        buffer_stderr = sys.stderr.buffer.write(b"buffer stderr\n")
        sys.stderr.buffer.flush()
        direct_stdout = os.write(1, b"direct stdout\n")
        direct_stderr = os.write(2, b"direct stderr\n")
        descendant_source = {descendant!r}
        exec_descendant = subprocess.run(
            [sys.executable, "-c", descendant_source],
            check=True,
        )
        """)
    expected = [
        "buffer stdout",
        "buffer stderr",
        "direct stdout",
        "direct stderr",
        "exec descendant stdout",
        "exec descendant stderr",
    ]
    output = _tool_text(client.send(python=python))
    output = client._collect_output(output, sum(len(line) + 1 for line in expected))
    assert sorted(output.splitlines()) == sorted(expected), repr(output)
    return client._finish()


def test_routes_r_console_channels(binary: Path) -> Transcript:
    client = RelayWorkerClient(binary)
    # fmt: r
    r = code(r"""
        cat("R output\n")
        message("R diagnostic")
        utils::file.edit(
          c("/dev/null", "/dev/null"),
          editor = Sys.which("true")
        )
        """)
    assert _tool_text(client.send(r=r)) == (
        "R output\nR diagnostic\nWARNING: Only editing the first in the list of files\n"
    )
    return client._finish()


def test_preserves_python_output_from_fork_children(binary: Path) -> Transcript:
    client = RelayWorkerClient(binary)
    # fmt: r
    r = code(r"""
        python <- Sys.which("python3")
        stopifnot(nzchar(python))
        reticulate::use_python(python, required = TRUE)
        suppressWarnings(invisible(reticulate::py_run_string("fork_ready = True")))
        """)
    assert _tool_text(client.send(r=r)) == "[done]"

    # fmt: python
    python = code(r"""
        import os
        import sys

        assert fork_ready
        child = os.fork()
        if child == 0:
            print("fork child stdout", flush=True)
            sys.stderr.write("fork child stderr\n")
            sys.stderr.flush()
            os._exit(0)

        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        parent_stdout = sys.stdout.write("parent stdout\n")
        parent_stderr = sys.stderr.write("parent stderr\n")
        """)
    expected = [
        "fork child stdout",
        "fork child stderr",
        "parent stdout",
        "parent stderr",
    ]
    output = _tool_text(client.send(python=python))
    output = client._collect_output(output, sum(len(line) + 1 for line in expected))
    assert sorted(output.splitlines()) == sorted(expected), repr(output)
    return client._finish()


def test_drains_standard_streams_while_evaluating(binary: Path) -> Transcript:
    client = RelayWorkerClient(binary)
    size = 4 * 1024 * 1024
    # fmt: python
    python = code(rf"""
        import os


        def write_all(file_descriptor, data):
            data = memoryview(data)
            while data:
                data = data[os.write(file_descriptor, data) :]


        write_all(1, b"x" * {size})
        write_all(2, b"y" * {size})
        """)
    output = _tool_text(client.send(python=python))
    output = client._collect_output(output, 2 * size)
    assert output.count("x") == size
    assert output.count("y") == size

    transcript = client._finish()
    assert transcript[-2] == {"stdout": "x" * size, "stderr": "y" * size}
    assert transcript[-1] == {"worker": {"kind": "completed"}}
    transcript[-2]["stdout"] = f"<{size} bytes>"
    transcript[-2]["stderr"] = f"<{size} bytes>"
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
