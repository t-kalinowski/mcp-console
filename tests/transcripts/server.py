#!/usr/bin/env -S uv run --script

from pathlib import Path
from textwrap import dedent

from _runner import run_this_suite
from _support import McpClient, Transcript


def test_initializes_lists_tools_and_calls_console(binary: Path) -> Transcript:
    client = McpClient(binary)
    client.initialize_and_list_tools()
    client.console(
        # fmt: r
        python=dedent("""
            print('hello')
        """).strip(),
        wait_ms=0,
    )
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
