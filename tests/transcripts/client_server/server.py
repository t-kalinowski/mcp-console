#!/usr/bin/env -S uv run --script

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import McpClient, Transcript, code, run_this_suite, stop_client


def assert_invalid_send_has_no_external_effects(binary: Path) -> None:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        fake_bin = temporary / "bin"
        fake_bin.mkdir()
        resolver_probe = fake_bin / "resolver-probe"
        resolver_probe.write_text(
            code(r"""
                #!/bin/sh

                set -eu
                printf 'resolver started\n' >> "$MCP_CONSOLE_TEST_RESOLVER_RECORD"
                exit 97
                """),
            encoding="utf-8",
        )
        resolver_probe.chmod(0o755)
        (fake_bin / "ir").symlink_to(resolver_probe)

        environment = os.environ.copy()
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        environment["RETICULATE_UV"] = str(resolver_probe)
        resolver_record = temporary / "resolver-record"
        worker_started = temporary / "zod-started"
        environment["MCP_CONSOLE_TEST_RESOLVER_RECORD"] = str(resolver_record)
        environment["MCP_CONSOLE_TEST_ZOD_STARTED"] = str(worker_started)

        client = McpClient(binary, ("serve", "--worker", str(zod)), environment)
        passed = False
        try:
            client._initialize_and_list_tools()
            invalid = (
                (
                    {"r": "echo invalid R cell ran", "requirements": {"r": [""]}},
                    "R requirement strings must not be empty",
                ),
                (
                    {
                        "python": "echo invalid Python cell ran",
                        "requirements": {
                            "python": ["example @ https://example.invalid/example.whl"]
                        },
                    },
                    (
                        "Python requirement `example @ "
                        "https://example.invalid/example.whl` is not accepted: "
                        "host-side managed resolution accepts named package "
                        "requirements only"
                    ),
                ),
                (
                    {
                        "sql": "echo invalid DuckDB cell ran",
                        "requirements": {"duckdb": ["spatial FROM community"]},
                    },
                    (
                        "DuckDB extension names must start with a lowercase ASCII "
                        "letter and contain only lowercase ASCII letters, digits, "
                        "and underscores"
                    ),
                ),
            )
            for arguments, expected in invalid:
                result = client.send(**arguments)
                assert result["isError"] is True, result
                assert result["content"] == [{"type": "text", "text": expected}], result

            assert not resolver_record.exists(), "invalid input started a host resolver"
            assert not worker_started.exists(), "invalid input started or ran a worker"
            client._finish()
            passed = True
        finally:
            if not passed:
                stop_client(client)


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
        "R package namespaces are resolved on demand",
        "Treat CRAN packages as available",
        "do not probe for installation or call `install.packages()`",
        "R source is not scanned in advance",
        "Successful automatic R and Python additions are cached, retained across cells, and reused after restart",
        "attaches it only through the user's original `library()` or `require()` call",
        "Use `requirements.r` only to stage packages ahead of time",
        "missing imports are resolved on demand",
        "curated mapping handles well-known differences between import names and PyPI distribution names",
        "distribution name matches the top-level module",
        "Import the packages best suited to the task instead of probing availability or running pip",
        "Python source is not scanned in advance",
        "Automatic Python imports infer only bare distribution names",
        "Use `requirements.python` when the distribution differs from the inferred name",
        "Explicit `requirements.python` accepts supported named PEP 508 registry requirements",
        "Explicit preparation does not load, import, or attach packages or extensions",
        "If you use a user-selected Python environment, import packages already installed there directly",
        "Call `send` sequentially",
        "ordinary console output",
        "cannot directly access the network",
        "whether triggered by Python imports or evaluated R code",
        "Automatic R discovery and Python import inference accept only plain names",
        "Managed Python version requests accept version numbers",
        "not interpreter selectors",
        "changes to `UV_*` made by evaluated code do not configure it",
        "nonempty user-selected `RETICULATE_PYTHON` disables automatic and explicit managed Python additions",
        "Package availability and system compatibility can still produce ordinary installation or load errors",
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
        "Use other CRAN packages directly",
        "`loadNamespace()`",
        "resolves missing plain package names on demand and retains successful additions",
        "does not attach it except through the original `library()` or `require()` call",
    ):
        assert guidance in r_description, guidance
    assert "ggplot2::" not in r_description
    assert "dplyr::" not in r_description
    assert "readr::" not in r_description
    assert "jsonlite::" not in r_description
    python_description = " ".join(
        send["inputSchema"]["properties"]["python"]["description"].split()
    )
    assert "`matplotlib.pyplot`" in python_description
    assert (
        "The built-in managed Python environment includes NumPy and pandas"
        in python_description
    )
    for guidance in (
        "Import other packages directly",
        "resolves a PyPI distribution on demand",
        "curated mapping for well-known import/distribution differences",
        "distribution matches the top-level module",
        "Successful additions are retained across cells and restart",
        "Python source is not scanned",
        "Use `requirements.python` when exact distribution metadata is needed",
        "user-selected Python environment disables both automatic resolution and managed requirements",
        "install packages into that environment or restart with managed Python enabled",
    ):
        assert guidance in python_description, guidance
    assert "bounded preview" in send["inputSchema"]["properties"]["sql"]["description"]
    send_requirements_description = " ".join(
        send["inputSchema"]["properties"]["requirements"]["description"].split()
    )
    for guidance in (
        "prepare before this cell and retain for later cells",
        "Ordinary CRAN packages used by the built-in R worker need not be declared here",
        "use `requirements.r` to stage packages ahead of evaluation",
        "missing imports normally resolve at runtime",
        "Use `requirements.python` to stage a distribution before the cell",
        "Python source is not pre-scanned",
        "SQL does not trigger package discovery",
        "The cell is not run if explicit preparation fails or further changes require restart",
        "Resolution runs outside the worker sandbox",
        "This field requires one `r`, `python`, or `sql` cell",
    ):
        assert guidance in send_requirements_description, guidance
    stdin_description = " ".join(
        send["inputSchema"]["properties"]["stdin"]["description"].split()
    )
    for guidance in (
        "Its UTF-8 encoding is queued exactly",
        "nonempty text is queued before the code is run",
        "an already waiting interactive read may consume it before the new cell begins",
        "Empty text queues nothing",
    ):
        assert guidance in stdin_description, guidance
    timeout_description = " ".join(
        send["inputSchema"]["properties"]["timeout_ms"]["description"].split()
    )
    for guidance in (
        "once the cell has been dispatched",
        "Requirement preparation happens first",
        "may make the complete call take longer",
        "Automatic R and Python import resolution are part of the running evaluation",
    ):
        assert guidance in timeout_description, guidance
    send_schema = json.dumps(send["inputSchema"])
    assert '"$defs"' not in send_schema, send["inputSchema"]
    assert '"$ref"' not in send_schema, send["inputSchema"]

    session = tools["session"]
    for guidance in (
        "Make additional R or Python packages and DuckDB extensions available",
        "prepares DuckDB's JSON and ICU extensions by default",
        "missing Python imports resolve automatically in the server-managed environment",
        "Import appropriate Python packages directly",
        "stage R packages ahead of a cell",
        "supply explicit IR references",
        "idle worker can add R requirements or DuckDB extensions",
        "compatible Python additions require a server-managed worker",
        "without losing live state",
        "evaluation remains available so state can be saved",
        "new requirement additions require restart",
        "successfully activated automatic R and Python additions are additive, idempotent, and persist across restart",
        "Preparation does not import, attach, or load packages or extensions",
        "active automatic R or Python resolver",
        "loses all in-memory R, Python, and SQL state",
        "Explicit managed Python additions accept named PEP 508 registry requirements",
        "paths, file URLs, editable requirements, direct references, local archives, and local projects are rejected",
        "server's startup `UV_*` configuration",
        "nonempty user-selected `RETICULATE_PYTHON` disables automatic resolution and managed Python requirements",
        "Automatic R discovery accepts only plain package names",
        "Use explicit `requirements.python` when exact distribution metadata is needed",
    ):
        assert guidance in session["description"], guidance
    session_schema = json.dumps(session["inputSchema"])
    assert '"$defs"' not in session_schema, session["inputSchema"]
    assert '"$ref"' not in session_schema, session["inputSchema"]
    send_requirements = send["inputSchema"]["properties"]["requirements"]
    session_requirements = session["inputSchema"]["properties"]["requirements"]
    send_requirements_shape = {
        key: value for key, value in send_requirements.items() if key != "description"
    }
    session_requirements_shape = {
        key: value
        for key, value in session_requirements.items()
        if key != "description"
    }
    assert send_requirements_shape == session_requirements_shape, (
        send_requirements,
        session_requirements,
    )
    assert send_requirements["type"] == ["object", "null"], send_requirements
    assert send_requirements["additionalProperties"] is False, send_requirements
    requirement_properties = send_requirements["properties"]
    assert requirement_properties.keys() == {"duckdb", "r", "python"}
    for requirement in requirement_properties.values():
        assert requirement["type"] == "array", requirement
        assert requirement["maxItems"] == 64, requirement
        assert requirement["default"] == [], requirement
        assert requirement["items"]["type"] == "string", requirement
        assert requirement["items"]["minLength"] == 1, requirement
    assert requirement_properties["duckdb"]["items"]["maxLength"] == 64
    action_description = " ".join(
        session["inputSchema"]["properties"]["action"]["description"].split()
    )
    assert "before a worker starts" in action_description, action_description
    assert (
        "After startup, it can add R requirements or DuckDB extensions while the worker is idle"
        in action_description
    ), action_description
    assert "`restart` can add any of the same requirements" in action_description, (
        action_description
    )
    requirements_description = " ".join(
        session["inputSchema"]["properties"]["requirements"]["description"].split()
    )
    assert "return `[restart required]` until restart" in requirements_description, (
        requirements_description
    )
    assert "evaluated code cannot configure that host resolver" in (
        requirements_description
    )
    assert (
        "Successfully activated automatic R and Python additions also persist across restart"
        in (requirements_description)
    )
    r_requirements_description = " ".join(
        session["inputSchema"]["properties"]["requirements"]["properties"]["r"][
            "description"
        ].split()
    )
    for guidance in (
        "stage packages ahead of evaluation",
        "explicit supported remote IR reference",
        "Automatic R discovery accepts only plain package names",
        "Local package sources are rejected",
    ):
        assert guidance in r_requirements_description, guidance
    python_requirements_description = " ".join(
        session["inputSchema"]["properties"]["requirements"]["properties"]["python"][
            "description"
        ].split()
    )
    for guidance in (
        "named PEP 508 registry requirements",
        "automatic import inference needs a different distribution",
        "a version, an extra, or an environment marker",
        "Automatic imports infer bare distribution names only",
        "Paths, file URLs, editable requirements, direct references, local archives, and local projects are rejected",
        "Preparation does not import the package",
        "nonempty user-selected `RETICULATE_PYTHON` disables automatic resolution and managed Python requirements",
    ):
        assert guidance in python_requirements_description, guidance
    duckdb_description = session["inputSchema"]["properties"]["requirements"][
        "properties"
    ]["duckdb"]["description"]
    assert (
        "JSON and ICU are already prepared for built-in workers" in duckdb_description
    )
    transcript = client._finish()
    assert not (workspace / ".mcp-console").exists(), workspace
    return transcript


def test_validates_send_arguments(binary: Path) -> Transcript:
    assert_invalid_send_has_no_external_effects(binary)
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(
        # fmt: python
        python=code("""
            print("hello")
        """),
        wait_ms=0,
    )
    result = client.send(
        r="1",
        python="1",
        sql="SELECT 1",
        requirements={"r": ["praise"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "only one of `r`, `python`, or `sql` may be supplied"
    ), result

    result = client.send(requirements={"r": ["praise"]})
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == "`requirements` requires a code cell", result

    result = client.send(stdin="answer\n", requirements={"r": ["praise"]})
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == "`requirements` requires a code cell", result

    result = client.send(r="stop('cell was run')", requirements={})
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "at least one of `requirements.r`, `requirements.python`, or "
        "`requirements.duckdb` is required"
    ), result

    result = client.send(
        r="stop('cell was run')",
        requirements={"r": [""]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == "R requirement strings must not be empty", (
        result
    )

    invalid_python = "example @ https://example.invalid/example.whl"
    result = client.send(
        r="stop('cell was run')",
        requirements={"python": [invalid_python]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        f"Python requirement `{invalid_python}` is not accepted: host-side managed "
        "resolution accepts named package requirements only"
    ), result

    result = client.send(
        r="stop('cell was run')",
        requirements={"duckdb": ["spatial FROM community"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "DuckDB extension names must start with a lowercase ASCII letter and "
        "contain only lowercase ASCII letters, digits, and underscores"
    ), result

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
        action="interrupt",
        requirements={"python": ["py-yaml12"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "`requirements` is not supported with `interrupt`"
    )

    client.session(
        action="restart",
        requirements={},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "at least one of `requirements.r`, `requirements.python`, or "
        "`requirements.duckdb` is required"
    )

    client.session(
        action="restart",
        requirements={"r": ["cli\ndplyr"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "R requirement strings must not contain NUL or line breaks"
    )

    client.session(
        action="restart",
        requirements={"duckdb": ["spatial FROM community"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "DuckDB extension names must start with a lowercase ASCII letter and "
        "contain only lowercase ASCII letters, digits, and underscores"
    )
    return client._finish()


def test_rejects_interrupt_without_worker(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.session(action="interrupt")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "worker is not running"
    return client._finish()


if __name__ == "__main__":
    run_this_suite(__file__)
