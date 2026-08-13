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
        "Language-native help and introspection are available",
        "`r.name`",
        "`py$name`",
        "SQL queries R data frames by name",
        "`sql_connection()`",
        "Use `session` to prepare missing packages",
        "Call `send` sequentially",
        "ordinary console output",
    ):
        assert guidance in send["description"], guidance
    assert (
        "Default-device plots" in send["inputSchema"]["properties"]["r"]["description"]
    )
    assert (
        "`matplotlib.pyplot`"
        in send["inputSchema"]["properties"]["python"]["description"]
    )
    assert "bounded preview" in send["inputSchema"]["properties"]["sql"]["description"]
    stdin_description = " ".join(
        send["inputSchema"]["properties"]["stdin"]["description"].split()
    )
    assert "Its UTF-8 encoding is queued to worker stdin exactly" in stdin_description

    session = tools["session"]
    for guidance in (
        "Prepare anticipated R packages before the worker starts",
        "returns `[restart required]` and applies none of that call's R or Python additions",
        "Packages are not imported or attached automatically",
        "loses all in-memory R, Python, and SQL state",
    ):
        assert guidance in session["description"], guidance
    session_schema = json.dumps(session["inputSchema"])
    assert '"$defs"' not in session_schema, session["inputSchema"]
    assert '"$ref"' not in session_schema, session["inputSchema"]
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
        "at least one of `requirements.r` or `requirements.python` is required"
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
    return client._finish()


if __name__ == "__main__":
    run_this_suite(__file__)
