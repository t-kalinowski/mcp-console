#!/usr/bin/env -S uv run --script

import os
import tempfile
from pathlib import Path

from _support import (
    McpClient,
    Transcript,
    code,
    normalize_python_resolution_error,
    r_test_environment,
    run_this_suite,
)


PLATFORMS = {"darwin"}
REQUIRED_COMMANDS = {"ir"}


def test_prepares_initial_r_requirements(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    initial_r = "local::tests/fixtures/r_require?reinstall"
    candidate_r = "local::tests/fixtures/r_require_candidate?reinstall"
    with tempfile.TemporaryDirectory() as temporary:
        ambient_library = Path(temporary) / "ambient-library"
        ambient_library.mkdir()
        environment["R_LIBS"] = os.pathsep.join(
            filter(None, (str(ambient_library), environment.get("R_LIBS")))
        )
        environment["MCP_CONSOLE_AMBIENT_R_LIBRARY"] = str(ambient_library)

        client = McpClient(binary, ("serve",), environment)
        client.initialize_and_list_tools()
        client.call_tool(
            "session",
            action="prepare",
            requirements={"r": [initial_r]},
        )
        assert last_tool_text(client) == "[prepared]"

        invalid_r = "not a valid requirement !!!"
        client.call_tool(
            "session",
            action="prepare",
            requirements={"r": [invalid_r]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        error = result["content"][0]["text"]
        assert error.startswith(
            "R package resolution failed with exit status: 1: Error:"
        ), error
        assert f"Cannot parse package: {invalid_r}." in error, error
        assert error.endswith("Execution halted\nir: dependency resolution failed"), (
            error
        )
        result["content"][0]["text"] = "\n".join(
            line.rstrip() for line in error.splitlines()
        )

        invalid_python = "not a valid requirement !!!"
        client.call_tool(
            "session",
            action="prepare",
            requirements={
                "r": [candidate_r],
                "python": [invalid_python],
            },
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        result["content"][0]["text"] = normalize_python_resolution_error(
            result["content"][0]["text"], invalid_python
        )

        # fmt: r
        r = code(r"""
            stopifnot(
              identical(
                dirname(find.package("mcpconsolerrequire")),
                .libPaths()[[1L]]
              ),
              !requireNamespace(
                "mcpconsolerrequirecandidate",
                quietly = TRUE
              ),
              normalizePath(.libPaths()[[2L]]) ==
                normalizePath(Sys.getenv("MCP_CONSOLE_AMBIENT_R_LIBRARY"))
            )
            mcpconsolerrequire::answer()
            """)
        client.call_tool("send", r=r)
        assert last_tool_text(client) == "[1] 42\n"

        client.call_tool(
            "session",
            action="prepare",
            requirements={"r": [initial_r]},
        )
        assert last_tool_text(client) == "[prepared]"
        client.call_tool(
            "session",
            action="prepare",
            requirements={"r": [candidate_r]},
        )
        assert last_tool_text(client) == "[restart required]"

        client.call_tool("session", action="restart")
        assert last_tool_text(client) == "[restarted]"
        client.call_tool("send", r=r)
        assert last_tool_text(client) == "[1] 42\n"
        return client.finish()


def last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    content = result["content"]
    assert len(content) == 1, content
    assert content[0]["type"] == "text", content
    return content[0]["text"]


if __name__ == "__main__":
    run_this_suite(__file__)
