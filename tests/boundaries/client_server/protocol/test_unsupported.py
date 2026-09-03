#!/usr/bin/env -S uv run --script

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.records import Transcript
from support.suites import run_this_suite

PLATFORMS = {"linux"}


def test_keeps_the_public_interface_without_starting_workers(
    binary: Path,
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("MCP_CONSOLE_LANGUAGES", None)
    client = McpClient(binary, ("serve",), environment)
    assert client.temporary_directory is not None
    workspace = Path(client.temporary_directory.name)
    client._initialize_and_list_tools()

    tools = client.transcript[-1]["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["send"], tools
    assert not (workspace / ".mcp-console").exists()

    removed = client._request(
        "tools/call",
        name="session",
        arguments={"action": "restart"},
    )
    assert removed["error"] == {"code": -32602, "message": "tool not found"}
    assert not (workspace / ".mcp-console").exists()

    evaluation = client.send(r="1 + 1")
    assert evaluation == {
        "content": [{"type": "text", "text": "[workers are supported only on macOS]"}],
        "isError": True,
    }, evaluation
    restart = client.send(control="restart")
    assert restart == {
        "content": [
            {
                "type": "text",
                "text": (
                    "[starting new worker]\n[workers are supported only on macOS]"
                ),
            }
        ],
        "isError": True,
    }, restart
    return client._finish()


if __name__ == "__main__":
    run_this_suite(__file__)
