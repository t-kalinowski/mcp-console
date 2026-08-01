#!/usr/bin/env -S uv run --script

from pathlib import Path
from textwrap import dedent

from _support import McpClient, Transcript, run_this_suite


PLATFORMS = {"darwin"}


def test_evaluates_a_complete_cell(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    client.call_tool(
        "send",
        r=dedent(
            # fmt: r
            r"""
            answer <- 40
            answer + 1
            answer + 2
            cat("done\n")
            invisible(99)
            """
        ).strip(),
    )
    client.call_tool("send", r='stop("boom", call. = FALSE)')
    client.call_tool("send", r="answer")
    client.call_tool("send", r="silent <- 1")
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
