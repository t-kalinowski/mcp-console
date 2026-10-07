"""Tool and field guidance follows visibility, not provider discovery."""

import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.assertions import last_tool_text
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.installation import installed_console
from support.linux_sandbox import retain_system_bwrap
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, R, SQL, requires
from support.resolvers import expose_uv
from support.snapshots import platform_snapshots
from support.suites import run_this_suite


def _descriptions(value: dict, path: str = "send") -> Iterator[tuple[str, str]]:
    for key, item in value.items():
        if key == "description":
            yield path, item
        elif isinstance(item, dict):
            yield from _descriptions(item, f"{path}.{key}")


def _without_descriptions(value: dict) -> dict:
    return {
        key: _without_descriptions(item) if isinstance(item, dict) else item
        for key, item in value.items()
        if key != "description"
    }


def _assert_guidance(tool: dict, languages: set[str], *, custom: bool) -> None:
    descriptions = dict(_descriptions(tool))
    for language in {"r", "python", "sql"} - languages:
        for path, description in descriptions.items():
            assert not re.search(
                rf"(?<![a-z]){language}(?![a-z])", description, re.I
            ), (language, path, description)
    description = tool["description"]
    assert "Cells are not transactional" in description
    assert "do not resubmit the cell" in description
    assert "use only trusted dependencies" in description
    assert ("Switch languages when useful" in description) == (len(languages) > 1)
    properties = tool["inputSchema"]["properties"]
    assert "discards" in properties["control"]["description"]
    assert "not rolled back" in " ".join(
        properties["requirements"]["description"].split()
    )
    if custom:
        assert "does not supply built-in runtime packages" in description
        assert "Managed requirements require" in description
    elif "sql" in languages:
        if languages != {"r", "python", "sql"}:
            assert "without a setup cell" in description
        assert "CSV, Parquet, JSON, and JSONL directly" in description
        assert "provider" in description.lower() or languages == {"r", "python", "sql"}
        sql = properties["sql"]["description"]
        assert "selected driver supplies its own SQL" in sql
        assert "CLI dot commands are not supported" in sql
        if "r" in languages:
            assert "With R-owned managed DuckDB" in sql
            assert "DBI" in sql
        if "python" in languages:
            assert "register(name, frame)" in sql
            python = properties["python"]["description"]
            assert "console_sql_connection(connection)" in python
            assert "register(name, frame)" in python
            if "r" not in languages:
                assert "only when" in python and "owns" in python


def _matrix(binary: Path, *, custom: bool) -> Transcript:
    records = []
    subsets = (
        (
            ("r", "python", "sql"),
            ("r",),
            ("python",),
            ("sql",),
            ("r", "python"),
            ("r", "sql"),
            ("python", "sql"),
        )
        if SQL.available
        else (("r", "python"), ("r",), ("python",))
    )
    for languages in subsets:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = dict(os.environ, MCP_CONSOLE_HOME=str(root / "console-home"))
            env.pop("MCP_CONSOLE_LANGUAGES", None)
            with McpClient(
                binary,
                DIRECT.serve(
                    *("--worker", "unused-worker") if custom else (),
                    "-c",
                    "languages=" + json.dumps(languages),
                ),
                env,
                root,
                record_in_project=False,
            ) as client:
                client.initialize_and_list_tools()
                (tool,) = client.transcript[-1]["result"]["tools"]
                _assert_guidance(tool, set(languages), custom=custom)
                schema = _without_descriptions(tool)
                if languages == subsets[0]:
                    baseline = schema
                else:
                    expected = _without_descriptions(baseline)
                    for field in {"r", "python", "sql"} - set(languages):
                        expected["inputSchema"]["properties"].pop(field, None)
                    assert schema == expected
                assert client.request("tools/list")["result"]["tools"] == [tool]
                client.finish()
                records.append(
                    {
                        "languages": list(languages),
                        "fields": list(schema["inputSchema"]["properties"]),
                    }
                )
    return records


@platform_snapshots("win32")
def test_builtin_visible_language_descriptions(binary: Path) -> Transcript:
    return _matrix(binary, custom=False)


@platform_snapshots("win32")
def test_custom_visible_language_descriptions(binary: Path) -> Transcript:
    return _matrix(binary, custom=True)


@requires(SQL, R, POSIX)
@executions(DIRECT, SANDBOXED)
def test_hidden_sql_provider_guidance(binary: Path, execution: Execution) -> Transcript:
    records = []
    for languages in (("sql",), ("python", "sql"), ("r", "sql")):
        advertised = None
        for without_r in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                env = dict(os.environ)
                env.pop("MCP_CONSOLE_LANGUAGES", None)
                if without_r:
                    commands = root / "commands"
                    commands.mkdir()
                    expose_uv(commands)
                    retain_system_bwrap(commands)
                    env["PATH"] = str(commands)
                    for name in (
                        "R_HOME",
                        "R_LIBS",
                        "R_LIBS_USER",
                        "RETICULATE_PYTHON",
                        "RETICULATE_UV",
                    ):
                        env.pop(name, None)
                with McpClient(
                    installed_console(binary),
                    execution.serve("-c", "languages=" + json.dumps(languages)),
                    env,
                    root,
                ) as client:
                    client.initialize_and_list_tools()
                    (tool,) = client.transcript[-1]["result"]["tools"]
                    _assert_guidance(tool, set(languages), custom=False)
                    if advertised is None:
                        advertised = tool
                    else:
                        assert tool == advertised
                    client.send(sql="SELECT 42 AS answer")
                    assert "42" in last_tool_text(client), last_tool_text(client)
                    if "python" in languages:
                        client.send(
                            python='print("sql_connection" in dir(__import__("builtins")))'
                        )
                        assert last_tool_text(client) == f"{without_r}\n"
                        if without_r:
                            client.send(
                                # fmt: python
                                python=code("""
                                    import pandas as pd

                                    frame = pd.DataFrame({"value": [19, 23]})
                                    _ = sql_connection().register("visible_frame", frame)
                                    """)
                            )
                        else:
                            client.send(
                                # fmt: python
                                python=code("""
                                    import sqlite3

                                    connection = sqlite3.connect(":memory:")
                                    _ = connection.execute("CREATE TABLE visible_frame(value INTEGER)")
                                    _ = connection.executemany("INSERT INTO visible_frame VALUES (?)", [(19,), (23,)])
                                    console_sql_connection(connection)
                                    """)
                            )
                        assert "Error" not in last_tool_text(client), last_tool_text(
                            client
                        )
                        client.send(
                            sql="SELECT sum(value) AS answer FROM visible_frame"
                        )
                        assert "42" in last_tool_text(client), last_tool_text(client)
                    assert client.request("tools/list")["result"]["tools"] == [tool]
                    transcript = client.finish()
                    records.append(
                        {
                            "languages": list(languages),
                            "provider": "Python" if without_r else "R",
                        }
                    )
                    records.extend(transcript[:3])
                    records.extend(entry for entry in transcript[3:] if "send" in entry)
    return records


@platform_snapshots("win32")
def test_missing_runtimes_keep_configured_descriptions(binary: Path) -> Transcript:
    records = []
    subsets = (
        (
            ("r",),
            ("python",),
            ("sql",),
            ("r", "python"),
            ("r", "sql"),
            ("python", "sql"),
            ("r", "python", "sql"),
        )
        if SQL.available
        else (("r",), ("python",), ("r", "python"))
    )
    for languages in subsets:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = dict(os.environ, PATH="")
            for name in (
                "R_HOME",
                "RETICULATE_PYTHON",
                "RETICULATE_UV",
                "MCP_CONSOLE_LANGUAGES",
            ):
                env.pop(name, None)
            with McpClient(
                binary,
                DIRECT.serve("-c", "languages=" + json.dumps(languages)),
                env,
                root,
                record_in_project=False,
            ) as client:
                client.initialize_and_list_tools()
                (tool,) = client.transcript[-1]["result"]["tools"]
                _assert_guidance(tool, set(languages), custom=False)
                assert set(tool["inputSchema"]["properties"]) & {
                    "r",
                    "python",
                    "sql",
                } == set(languages)
                result = client.send(timeout_ms=10_000)
                diagnostic = "Python sessions without R require `uv` on PATH; set python in .agents/console/config.yaml to use an existing environment"
                assert result == {
                    "content": [{"type": "text", "text": diagnostic}],
                    "isError": True,
                }, result
                assert client.request("tools/list")["result"]["tools"] == [tool]
                _, stderr = client.finish_with_standard_error(expected_exit_status=1)
                assert stderr == diagnostic + "\n", stderr
                records.append(
                    {"languages": list(languages), "preparation_error": result}
                )
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
