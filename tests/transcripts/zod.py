#!/usr/bin/env -S uv run --script

import os
import subprocess
import tempfile
import time
from pathlib import Path

from _support import McpClient, Transcript, run_this_suite


PLATFORMS = {"darwin", "linux"}


def test_routes_send_over_sideband(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client.initialize_and_list_tools()
    client.call_tool("send", r="hello")
    return client.finish()


def test_shuts_down_stalled_worker(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        environment = os.environ.copy()
        stalled = Path(temporary_directory) / "zod-stalled"
        environment["ZOD_STALL_PATH"] = str(stalled)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        client.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "send",
                    "arguments": {"r": "stall"},
                },
            }
        )
        wait_for_stall(stalled, client)
        client.stdin.close()
        try:
            return_code = client.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            client.process.kill()
            client.process.wait()
            raise AssertionError(
                "mcp-console did not stop its stalled worker"
            ) from None

        assert return_code == 0, client.stderr.read()
        client.stdout.read()
        assert client.stderr.read() == ""
        return client.transcript


def wait_for_stall(stalled: Path, client: McpClient) -> None:
    deadline = time.monotonic() + 3
    while not stalled.exists():
        assert client.process.poll() is None, "mcp-console stopped before Zod stalled"
        assert time.monotonic() < deadline, "Zod did not report its stall checkpoint"
        time.sleep(0.01)


if __name__ == "__main__":
    run_this_suite(__file__)
