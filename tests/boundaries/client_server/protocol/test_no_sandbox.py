#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import tool_text
from support.client import McpClient
from support.records import Transcript
from support.suites import run_this_suite

PLATFORMS = {"darwin", "linux"}


def test_evaluates_without_filesystem_isolation(binary: Path) -> Transcript:
    with McpClient(binary, ("serve", "--no-sandbox")) as client:
        client.initialize_and_list_tools()
        assert client.temporary_directory is not None
        workspace = Path(client.temporary_directory.name)
        assert (
            tool_text(client.send(r='writeLines("from R", "result.txt"); answer <- 42'))
            == "[done]"
        )
        assert (workspace / "result.txt").read_text() == "from R\n"
        assert tool_text(client.send(r="answer")) == "[1] 42\n"
        assert (
            tool_text(client.send(control="restart"))
            == "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        assert tool_text(client.send(r='exists("answer")')) == "[1] FALSE\n"
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
