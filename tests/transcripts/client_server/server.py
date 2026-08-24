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
    assert_limits_send_languages_from_environment(binary)
    environment = os.environ.copy()
    environment.pop("MCP_CONSOLE_LANGUAGES", None)
    client = McpClient(binary, ("serve",), environment)
    assert client.temporary_directory is not None
    workspace = Path(client.temporary_directory.name)
    client._initialize_and_list_tools()
    tools = {tool["name"]: tool for tool in client.transcript[-1]["result"]["tools"]}
    send = tools["send"]
    send_description = " ".join(send["description"].split())
    for guidance in (
        "Persistent R, Python, and DuckDB SQL workbench",
        "State persists across sequential calls",
        "Reassess the language for each cell and switch whenever another language is a better fit",
        "do not stay in one language solely because state already exists there",
        "Use the available live bridges when switching",
        "Send one complete `r`, `python`, or `sql` cell per call",
        "Calls must be sequential because only one evaluation can be active",
        "leave the primary result last",
        "R and Python display a final visible top-level expression",
        "SQL returns a bounded preview",
        "Cells are not transactional",
        "`timeout_ms` limits how long the call waits",
        "after dispatch or attachment",
        "explicit requirement preparation can make the complete call take longer",
        "does not cancel startup, dependency resolution, or evaluation",
        "[running; poll with an empty send]",
        "call `send` again without code or stdin",
        "do not resubmit the cell",
        "Send `stdin` without code to answer an active prompt or debugger",
        '`session(action = "interrupt")` to request interruption',
        "`r.name`",
        "`py$name`",
        "SQL can query R data frames by name",
        "`sql_connection()`",
        "resolves ordinary CRAN packages and missing imports",
        "Use `requirements` for explicit R references, exact Python distribution metadata, or DuckDB extensions",
        "preparation makes dependencies available but does not import, attach, or load them",
        "open `matplotlib.pyplot` figures return as PNG images",
        "cannot directly access the network",
        "write only in the worker's private temporary directory",
        "Dependency resolution runs outside the sandbox",
        "use only trusted dependencies",
    ):
        assert guidance in send_description, guidance
    for tutorial in (
        "```",
        "ls.str()",
        "reticulate::py_run_string",
        'DBI::dbGetQuery(sql_connection(), "SHOW TABLES")',
        'stdin = "sys.calls()\\n"',
        'stdin = "c\\n"',
        "Inspect warnings before relying on a result",
        "coercion, overflow, dropped observations, or model convergence",
        "200-column startup width",
        "Responses remain ordinary text and image content",
    ):
        assert tutorial not in send_description, tutorial
    r_description = " ".join(
        send["inputSchema"]["properties"]["r"]["description"].split()
    )
    for guidance in (
        "One complete R cell",
        "persistent global state",
        "final visible expression autoprints",
        "Leave the primary result last",
        "missing plain CRAN package names on demand",
        "`loadNamespace()`",
        "do not probe package availability or call `install.packages()`",
        "`py$name`",
        "R data frames are directly queryable by name from later SQL cells",
        "borrowed `sql_connection()`",
        "do not disconnect it",
        "Default-device plots return as PNG images",
        "`options(console.plot.width",
        "Omit this field for polling or stdin-only calls",
    ):
        assert guidance in r_description, guidance
    python_description = " ".join(
        send["inputSchema"]["properties"]["python"]["description"].split()
    )
    for guidance in (
        "One complete Python cell",
        "persistent `__main__` state",
        "final visible expression autoprints",
        "Leave the primary result last",
        "When an import is missing",
        "resolves a PyPI distribution on demand",
        "curated mapping for well-known import/distribution differences",
        "distribution matches the top-level module",
        "Use `requirements.python` when the distribution differs from the inferred name",
        "exact registry metadata is needed",
        "user-selected Python environment disables both automatic resolution and managed requirements",
        "`r.name`",
        "bind them to an R name first",
        "open `matplotlib.pyplot` figure returns once as a PNG image and is closed",
        "Omit this field for polling or stdin-only calls",
    ):
        assert guidance in python_description, guidance
    sql_description = " ".join(
        send["inputSchema"]["properties"]["sql"]["description"].split()
    )
    for guidance in (
        "One complete DuckDB SQL cell",
        "persistent catalog",
        "final query result returns a bounded preview",
        "unqualified relation name can query a data frame in R global state",
        "DuckDB table or view with the same name takes precedence",
        "`SHOW TABLES`",
        "outside the worker's private temporary directory",
        "`ATTACH 'path' AS name (READ_ONLY)`",
        "the sandbox blocks DuckDB's default writable mode for those paths",
        "DuckDB CLI dot commands are not supported",
        "Omit this field for polling or stdin-only calls",
    ):
        assert guidance in sql_description, guidance
    send_requirements_description = " ".join(
        send["inputSchema"]["properties"]["requirements"]["description"].split()
    )
    for guidance in (
        "additive and persist for the session",
        "prepare before this cell and retain for later cells",
        "Ordinary CRAN packages used by the built-in R worker need not be declared here",
        "use `requirements.r` to stage packages ahead of evaluation",
        "missing imports normally resolve at runtime",
        "Use `requirements.python` to stage a distribution before the cell",
        "SQL does not trigger package discovery",
        "The cell is not run if explicit preparation fails or further changes require restart",
        "Resolution runs outside the worker sandbox",
        "Use only trusted requirements",
        "This field requires one `r`, `python`, or `sql` cell",
    ):
        assert guidance in send_requirements_description, guidance
    stdin_description = " ".join(
        send["inputSchema"]["properties"]["stdin"]["description"].split()
    )
    for guidance in (
        "Input for an active read, prompt, or debugger",
        "omit R, Python, and SQL code",
        "Its UTF-8 encoding is queued exactly",
        "no newline is added",
        "trailing `\\n`",
        "If requirements are also supplied, preparation completes first",
        "When sent with a cell, nonempty text is queued before the code is run",
        "an already waiting interactive read may consume it before the new cell begins",
        "[waiting for stdin]",
        "Unread text can satisfy later reads and is discarded by restart",
    ):
        assert guidance in stdin_description, guidance
    assert "When sent with requirements and a cell" not in stdin_description
    assert "sys.calls()" not in stdin_description
    timeout_description = " ".join(
        send["inputSchema"]["properties"]["timeout_ms"]["description"].split()
    )
    for guidance in (
        "Maximum time this call waits",
        "does not cancel evaluation",
        "poll with an empty `send` call",
        "once a cell has been dispatched or the call has attached",
        "Requirement preparation happens first",
        "may make the complete call take longer",
        "Automatic R and Python import resolution are part of the running evaluation",
        "do not resubmit the cell",
    ):
        assert guidance in timeout_description, guidance
    send_schema = json.dumps(send["inputSchema"])
    assert '"$defs"' not in send_schema, send["inputSchema"]
    assert '"$ref"' not in send_schema, send["inputSchema"]

    session = tools["session"]
    session_description = " ".join(session["description"].split())
    for guidance in (
        "Manage dependencies and the lifecycle of the persistent worker",
        "`prepare` makes additional R or Python requirements or DuckDB extensions available without evaluating a cell",
        "`interrupt` requests SIGINT for the active host resolver or live worker and returns after sending the request",
        "interruption is cooperative",
        "if an evaluation remains active, use an empty `send` afterward to observe whether it stopped",
        "`restart` optionally prepares requirements, replaces the worker, and discards all in-memory R, Python, SQL, debugger, and unread-stdin state",
        "Requirements are additive, idempotent, and persist across restart",
        "Preparation does not import, attach, or load packages or extensions",
        "Use `restart` only when clean state or a restart-required dependency change is needed",
        "ordinary language errors normally leave the worker reusable",
        "Dependency resolution runs outside the sandbox",
        "use only trusted requirements",
    ):
        assert guidance in session_description, guidance
    assert "Inspect partial state before retrying" not in session_description
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


def assert_limits_send_languages_from_environment(binary: Path) -> None:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment = os.environ.copy()
    environment["MCP_CONSOLE_LANGUAGES"] = "r,sql"
    client_directory = tempfile.TemporaryDirectory()
    worker_started = Path(client_directory.name) / "zod-started"
    environment["MCP_CONSOLE_TEST_ZOD_STARTED"] = str(worker_started)
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
        environment,
    )
    client._initialize_and_list_tools()

    tools = {tool["name"]: tool for tool in client.transcript[-1]["result"]["tools"]}
    send_properties = tools["send"]["inputSchema"]["properties"]
    assert send_properties.keys() == {
        "r",
        "sql",
        "requirements",
        "stdin",
        "timeout_ms",
    }
    result = client.send(python="raise AssertionError('disabled cell ran')")
    assert result["isError"] is True, result
    assert result["content"] == [
        {
            "type": "text",
            "text": "`python` cells are disabled by `MCP_CONSOLE_LANGUAGES`",
        }
    ], result
    assert not worker_started.exists(), worker_started
    client._finish()
    client_directory.cleanup()


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
