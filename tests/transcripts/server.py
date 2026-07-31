#!/usr/bin/env -S uv run --script

from pathlib import Path
from _support import McpClient, Transcript, run_this_suite


def test_initializes_lists_tools_and_calls_send(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    client.call_tool("send")
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
