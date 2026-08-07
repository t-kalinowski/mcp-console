#!/usr/bin/env -S uv run --script

import json
import os
import tempfile
import time
from pathlib import Path
from typing import TextIO

from _support import McpClient, Transcript, code, run_this_suite


PLATFORMS = {"darwin"}
CAPTURE_NAME = "mcp-console-worker-wire.jsonl"
CAPTURE_STDIN_CLOSE_ENV = "MCP_CONSOLE_MITM_CAPTURE_STDIN_CLOSE"


class WorkerWireClient:
    def __init__(self, binary: Path, *, capture_stdin_close: bool = False) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        environment = os.environ.copy()
        environment["TMPDIR"] = str(root)
        environment["MCP_CONSOLE_MITM_WORKER"] = str(binary)
        if capture_stdin_close:
            environment[CAPTURE_STDIN_CLOSE_ENV] = "1"
        mitm = Path(__file__).resolve().parents[1] / "fixtures" / "worker_mitm"
        self.client = McpClient(
            binary,
            ("serve", "--worker", str(mitm)),
            environment,
        )
        self.client.initialize_and_list_tools()

    def call_tool(self, **arguments: object) -> str:
        self.client.call_tool("send", **arguments)
        result = self.client.transcript[-1]["result"]
        assert result.get("isError") is not True, result
        return result["content"][0]["text"]

    def restart(self) -> str:
        self.client.call_tool("session", action="restart")
        result = self.client.transcript[-1]["result"]
        assert result.get("isError") is not True, result
        return result["content"][0]["text"]

    def open_capture(self) -> tuple[Path, TextIO]:
        capture = self.capture_path()
        return capture, capture.open(encoding="utf-8")

    def collect_output(self, output: str, expected_size: int) -> str:
        chunks = [] if output == "[done]" else [output]
        deadline = time.monotonic() + 10
        while sum(map(len, chunks)) < expected_size:
            assert time.monotonic() < deadline, sum(map(len, chunks))
            polled = self.call_tool()
            assert polled.endswith("\n[idle]"), repr(polled)
            chunks.append(polled.removesuffix("\n[idle]"))
        output = "".join(chunks)
        assert len(output) == expected_size, len(output)
        return output

    def finish(self) -> Transcript:
        transcript = self.read_capture(self.capture_path())
        self.client.finish()
        self.temporary.cleanup()
        return transcript

    def finish_restart(
        self,
        old_path: Path,
        old_capture: TextIO,
    ) -> Transcript:
        transcript = self.read_open_capture(old_capture)
        transcript.extend(self.read_capture(self.capture_path(excluding=old_path)))
        old_capture.close()
        self.client.finish()
        self.temporary.cleanup()
        return transcript

    def capture_path(self, excluding: Path | None = None) -> Path:
        root = Path(self.temporary.name)
        captures = [
            path
            for path in root.glob(f"mcp-console-tmp-*/{CAPTURE_NAME}")
            if path != excluding
        ]
        assert len(captures) == 1, captures
        return captures[0]

    @staticmethod
    def read_capture(capture: Path) -> Transcript:
        return [
            json.loads(line)
            for line in capture.read_text(encoding="utf-8").splitlines()
        ]

    @staticmethod
    def read_open_capture(capture: TextIO) -> Transcript:
        return [json.loads(line) for line in capture.read().splitlines()]


def test_restarts_session(binary: Path) -> Transcript:
    client = WorkerWireClient(binary, capture_stdin_close=True)
    assert client.restart() == "[restarted]"
    old_path, old_capture = client.open_capture()

    assert client.restart() == "[restarted]"
    transcript = client.finish_restart(old_path, old_capture)
    assert {"stdin": {"closed": True}} in transcript
    return transcript


def test_routes_python_output(binary: Path) -> Transcript:
    client = WorkerWireClient(binary)
    # fmt: r
    r = code(r"""
        suppressWarnings(
          invisible(reticulate::py_run_string("initialized_from_r = True"))
        )
        """)
    assert client.call_tool(r=r) == "[done]"

    # fmt: python
    python = code(r"""
        import sys

        assert initialized_from_r
        print("Python stdout")
        sys.stderr.write("Python stderr\n")
        raise ValueError("boom")
        """)
    output = client.call_tool(python=python)
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
    output = client.call_tool(python=python)
    output = client.collect_output(output, sum(len(line) + 1 for line in expected))
    assert sorted(output.splitlines()) == sorted(expected), repr(output)
    return client.finish()


def test_preserves_python_output_from_fork_children(binary: Path) -> Transcript:
    client = WorkerWireClient(binary)
    # fmt: r
    r = code(r"""
        python <- Sys.which("python3")
        stopifnot(nzchar(python))
        reticulate::use_python(python, required = TRUE)
        suppressWarnings(invisible(reticulate::py_run_string("fork_ready = True")))
        """)
    assert client.call_tool(r=r) == "[done]"

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
        print("parent stdout")
        parent_stderr = sys.stderr.write("parent stderr\n")
        """)
    expected = [
        "fork child stdout",
        "fork child stderr",
        "parent stdout",
        "parent stderr",
    ]
    output = client.call_tool(python=python)
    output = client.collect_output(output, sum(len(line) + 1 for line in expected))
    assert sorted(output.splitlines()) == sorted(expected), repr(output)
    return client.finish()


def test_drains_standard_streams_while_evaluating(binary: Path) -> Transcript:
    client = WorkerWireClient(binary)
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
    output = client.call_tool(python=python)
    output = client.collect_output(output, 2 * size)
    assert output.count("x") == size
    assert output.count("y") == size

    transcript = client.finish()
    assert transcript[-2] == {"stdout": "x" * size, "stderr": "y" * size}
    assert transcript[-1] == {"worker": {"kind": "completed"}}
    transcript[-2]["stdout"] = f"<{size} bytes>"
    transcript[-2]["stderr"] = f"<{size} bytes>"
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
