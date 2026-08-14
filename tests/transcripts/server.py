#!/usr/bin/env -S uv run --script

import json
import os
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
        "If you want to use a package",
        "prepare it with `session`",
        "load it directly with R `library()` or Python `import`",
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
        "Make additional R or Python packages available",
        "Do not probe package availability in cells",
        "If you want to use a package",
        "use `prepare`",
        "load it with R `library()` or Python `import` in `send`",
        "idle server-managed worker can add R and compatible Python requirements",
        "without losing live state",
        "evaluation remains available so state can be saved",
        "new requirement additions require restart",
        "Packages are not imported or attached automatically",
        "loses all in-memory R, Python, and SQL state",
    ):
        assert guidance in session["description"], guidance
    session_schema = json.dumps(session["inputSchema"])
    assert '"$defs"' not in session_schema, session["inputSchema"]
    assert '"$ref"' not in session_schema, session["inputSchema"]
    action_description = " ".join(
        session["inputSchema"]["properties"]["action"]["description"].split()
    )
    assert "before a server-managed worker starts" in action_description, (
        action_description
    )
    assert (
        "After startup, it can add R and compatible Python requirements while the worker is idle"
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


def test_initializes_and_lists_tools_with_custom_worker(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    tools = {tool["name"]: tool for tool in client.transcript[-1]["result"]["tools"]}

    send = tools["send"]
    for guidance in (
        "custom worker selected with `serve --worker`",
        "custom worker defines the supported languages and installed packages",
        "Package availability and loading are worker-defined",
        "package preparation with `session` is unavailable",
    ):
        assert guidance in send["description"], guidance
    assert "prepare it with `session`" not in send["description"]
    for language in ("r", "python", "sql"):
        description = send["inputSchema"]["properties"][language]["description"]
        assert f"custom worker with the `{language}` language tag" in description
        assert "worker defines how the source is evaluated" in description

    session = tools["session"]
    for guidance in (
        "Restart the persistent custom-worker session",
        'action = "restart"',
        "package preparation and restart-time requirements are unavailable",
        "loses all worker-owned state and unread stdin",
    ):
        assert guidance in session["description"], guidance
    properties = session["inputSchema"]["properties"]
    assert properties["action"]["enum"] == ["restart"], properties["action"]
    assert "replaces the custom worker" in properties["action"]["description"]
    assert "requirements" not in properties
    advertised = json.dumps(tools)
    for built_in_claim in (
        "The default R environment",
        "The built-in managed Python environment",
        "DuckDB SQL is also available",
        "server-managed worker",
    ):
        assert built_in_claim not in advertised, built_in_claim
    return client._finish()


def test_initializes_and_lists_tools_with_configured_python(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = "configured-by-user"
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    tools = {tool["name"]: tool for tool in client.transcript[-1]["result"]["tools"]}

    send = tools["send"]
    for guidance in (
        "The default R environment includes tidyverse, reticulate, DBI, and duckdb",
        "Python initially follows inherited `RETICULATE_PYTHON` configuration",
        "A successful `prepare` with Python requirements before the worker starts",
        "Import packages provided by the active Python environment directly",
        "If you want to use an additional package, prepare it with `session`",
        "If Python preparation reports `[restart required]`",
        "When managed Python is active",
    ):
        assert guidance in send["description"], guidance
    assert (
        "managed Python environment includes NumPy and pandas"
        not in send["description"]
    )
    python = send["inputSchema"]["properties"]["python"]["description"]
    assert "initially follows inherited `RETICULATE_PYTHON` configuration" in python
    assert (
        "Import packages provided by the active Python environment directly" in python
    )

    session = tools["session"]
    assert (
        "Load packages provided by the active Python environment directly"
        in session["description"]
    )
    assert (
        "If Python preparation reports `[restart required]`" in session["description"]
    )
    action = session["inputSchema"]["properties"]["action"]["description"]
    assert (
        "After an inherited Python worker starts, use `restart` with Python requirements"
        in action
    )
    python_requirements = session["inputSchema"]["properties"]["requirements"][
        "properties"
    ]["python"]["description"]
    assert (
        "After an inherited Python worker starts, supply additions to `restart`"
        in python_requirements
    )
    assert "requirements" in session["inputSchema"]["properties"]
    return client._finish()


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
