#!/usr/bin/env -S uv run --script

import json
from pathlib import Path

from _support import McpClient, Transcript, code, run_this_suite


def test_initializes_and_lists_tools(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    assert client.temporary_directory is not None
    workspace = Path(client.temporary_directory.name)
    client._initialize_and_list_tools()
    tools = {tool["name"]: tool for tool in client.transcript[-1]["result"]["tools"]}
    send = tools["send"]
    for guidance in (
        "Use it whenever exact computation or direct inspection would improve accuracy",
        "arithmetic, string counting, parsing",
        "Choose the clearest language for each step",
        "The default R environment includes tidyverse, reticulate, DBI, and duckdb",
        "their full dependency sets",
        "The built-in managed Python environment includes NumPy and pandas",
        "Language-native help and introspection are available",
        "`r.name`",
        "`py$name`",
        "SQL queries R data frames by name",
        "`sql_connection()`",
        "Do not probe package availability in cells",
        "Use `session` to prepare other packages",
        "If you use a custom Python installation, import packages already installed there directly",
        "Call `send` sequentially",
        "ordinary console output",
        "cannot directly access the network",
        "Managed Python requirement resolution triggered by R code",
    ):
        assert guidance in send["description"], guidance
    assert (
        "Default-device plots" in send["inputSchema"]["properties"]["r"]["description"]
    )
    r_description = " ".join(
        send["inputSchema"]["properties"]["r"]["description"].split()
    )
    for guidance in (
        "tidyverse, reticulate, DBI, duckdb, and their full dependency sets",
        "Packages are not attached automatically",
    ):
        assert guidance in r_description, guidance
    assert "ggplot2::" not in r_description
    assert "dplyr::" not in r_description
    assert "readr::" not in r_description
    assert "jsonlite::" not in r_description
    assert (
        "`matplotlib.pyplot`"
        in send["inputSchema"]["properties"]["python"]["description"]
    )
    assert (
        "The built-in managed Python environment includes NumPy and pandas"
        in send["inputSchema"]["properties"]["python"]["description"]
    )
    assert "bounded preview" in send["inputSchema"]["properties"]["sql"]["description"]
    stdin_description = " ".join(
        send["inputSchema"]["properties"]["stdin"]["description"].split()
    )
    assert "Its UTF-8 encoding is queued to worker stdin exactly" in stdin_description

    session = tools["session"]
    for guidance in (
        "Make additional R or Python packages and DuckDB extensions available",
        "packages not included in the built-in environments",
        "idle worker can add R requirements or DuckDB extensions",
        "compatible Python additions require a server-managed worker",
        "without losing live state",
        "evaluation remains available so state can be saved",
        "new requirement additions require restart",
        "Packages and extensions are not imported, attached, or loaded automatically by preparation",
        "loses all in-memory R, Python, and SQL state",
    ):
        assert guidance in session["description"], guidance
    session_schema = json.dumps(session["inputSchema"])
    assert '"$defs"' not in session_schema, session["inputSchema"]
    assert '"$ref"' not in session_schema, session["inputSchema"]
    action_description = " ".join(
        session["inputSchema"]["properties"]["action"]["description"].split()
    )
    assert "before a worker starts" in action_description, action_description
    assert (
        "After startup, it can add R requirements or DuckDB extensions while the worker is idle"
        in action_description
    ), action_description
    requirements_description = session["inputSchema"]["properties"]["requirements"][
        "description"
    ]
    assert "return `[restart required]` until restart" in requirements_description, (
        requirements_description
    )
    transcript = client._finish()
    assert not (workspace / ".mcp-console").exists(), workspace
    return transcript


def test_validates_send_arguments(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(
        # fmt: python
        python=code("""
            print("hello")
        """),
        wait_ms=0,
    )
    client.send(r="1", python="1", sql="SELECT 1")
    client.send(r=None)
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "\n[idle]", output
    return client._finish()


def test_validates_session_arguments(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.session(action="prepare")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == ("`requirements` is required with `prepare`")

    client.session(action="prepare", requirements={})
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "at least one of `requirements.r`, `requirements.python`, or "
        "`requirements.duckdb` is required"
    )

    client.session(
        action="prepare",
        requirements={"duckdb": ["spatial FROM community"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "DuckDB extension names must start with a lowercase ASCII letter and "
        "contain only lowercase ASCII letters, digits, and underscores"
    )

    client.session(action="prepare", requirements={"r": [""]})
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "R requirement strings must not be empty"

    client.session(
        action="prepare",
        requirements={"r": ["cli\ndplyr"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "R requirement strings must not contain NUL or line breaks"
    )

    client.session(
        action="restart",
        requirements={"python": []},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "`requirements.python` must contain at least one requirement"
    )

    client.session(
        action="restart",
        requirements={"r": ["cli"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "`requirements.r` is not supported with `restart`"
    )

    client.session(
        action="restart",
        requirements={"duckdb": ["json"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "`requirements.duckdb` is not supported with `restart`"
    )
    return client._finish()


if __name__ == "__main__":
    run_this_suite(__file__)
