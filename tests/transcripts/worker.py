#!/usr/bin/env -S uv run --script

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from _support import McpClient, Transcript, code, run_this_suite


PLATFORMS = {"darwin"}
CAPTURE_NAME = "mcp-console-worker-wire.jsonl"


class WorkerWireClient:
    def __init__(self, binary: Path) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        environment = os.environ.copy()
        python = shutil.which("python3")
        assert python is not None
        environment["RETICULATE_PYTHON"] = python
        environment["TMPDIR"] = str(root)
        environment["MCP_CONSOLE_MITM_WORKER"] = str(binary)
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
        root = Path(self.temporary.name)
        captures = list(root.glob(f"mcp-console-tmp-*/{CAPTURE_NAME}"))
        assert len(captures) == 1, captures
        transcript = [
            json.loads(line)
            for line in captures[0].read_text(encoding="utf-8").splitlines()
        ]
        self.client.finish()
        self.temporary.cleanup()
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
