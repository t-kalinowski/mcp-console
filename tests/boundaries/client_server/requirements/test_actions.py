#!/usr/bin/env -S uv run --script

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient, stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite
from support.requirements import command, requires
from support.resolvers import checkpoint_uv_environment, ir_run_records
from boundaries.client_server.requirements.test_r_automatic import (
    recording_fixture_r_environment,
)
from support.checkpoints import FifoCheckpoint


def inspect(client: McpClient) -> dict:
    result = client.send(requirements={"action": "get"})
    assert not result.get("isError"), result
    snapshot = result["structuredContent"]
    assert json.loads(last_tool_text(client)) == snapshot
    return snapshot


@executions(DIRECT, SANDBOXED)
def test_empty_declaration_and_round_trip(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    startup = inspect(client)
    assert startup["prepared"] is False
    assert startup["requirements"]["python"] == ["numpy", "pandas"]
    assert "tidyverse" in startup["requirements"]["r"]
    assert startup["runtime_requirements"]["python"] == []
    client.send(requirements=dict(startup["requirements"], action="set"))
    assert inspect(client) == startup
    client.send(requirements={"action": "reset"})
    assert inspect(client) == startup

    result = client.send(requirements={"action": "set"})
    assert not result.get("isError"), result
    empty = inspect(client)
    assert empty["prepared"] is True
    assert empty["requirements"] == {
        "r": [],
        "python": [],
        "duckdb": [],
        "python_version": [],
        "exclude_newer": None,
    }
    # fmt: python
    python = code("""
        import importlib.util
        import importlib.metadata
        import json
        import os
        import sys

        assert not {"numpy", "pandas"} & {
            d.metadata["Name"] for d in importlib.metadata.distributions()
        }
        marker = 42
        original_pid = os.getpid()
        (
            json.loads("42"),
            importlib.util.find_spec("numpy"),
            importlib.util.find_spec("pandas"),
        )
        """)
    client.send(python=python)
    assert last_tool_text(client) == "(42, None, None)\n"
    assert inspect(client) == empty
    client.send(r="stopifnot(identical(2L + 2L, 4L))")
    client.send(sql="SELECT 42 AS answer")
    assert inspect(client) == empty
    client.send(control="restart")
    client.send(python=python)
    assert last_tool_text(client) == "(42, None, None)\n"
    assert inspect(client) == empty

    edited = dict(
        empty["requirements"],
        python=["six"],
        python_version=[">=3.11", "<3.14"],
        exclude_newer="2026-01-01",
    )
    result = client.send(requirements=dict(edited, action="set"))
    assert result.get("isError"), result
    assert 'control="restart"' in result["content"][0]["text"]
    client.send(python="marker, os.getpid() == original_pid")
    assert last_tool_text(client) == "(42, True)\n"
    assert inspect(client) == empty

    result = client.send(
        control="restart",
        requirements=dict(edited, action="set"),
        python="import six; six.__name__",
    )
    assert not result.get("isError"), result
    assert "'six'" in last_tool_text(client)
    selected = inspect(client)
    assert selected["requirements"] == dict(
        edited, python_version=sorted(edited["python_version"])
    )
    client.send(requirements=dict(selected["requirements"], action="set"))
    assert last_tool_text(client) == "[prepared]"
    client.send(python="marker = 42")
    client.send(
        control="restart",
        requirements=dict(selected["requirements"], action="set"),
        python='"marker" in globals()',
    )
    assert "False" in last_tool_text(client)
    assert inspect(client) == selected

    client.send(
        control="restart",
        requirements={"action": "set", "r": [], "python": [], "duckdb": []},
    )
    assert inspect(client) == empty
    client.send(requirements={"python": ["six"]})
    added = inspect(client)
    assert added["requirements"] == dict(empty["requirements"], python=["six"])
    client.send(requirements={"action": "add", "python": ["six", "six"]})
    assert inspect(client) == added
    client.send(control="restart")
    assert inspect(client) == added
    client.send(
        control="restart",
        requirements={"python_version": [">=3.11"], "exclude_newer": "2026-01-01"},
    )
    assert inspect(client)["requirements"] == dict(
        added["requirements"], python_version=[">=3.11"], exclude_newer="2026-01-01"
    )
    client.send(control="restart", requirements={"action": "reset"})
    assert inspect(client)["requirements"] == startup["requirements"]
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_inspection_validation(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    initial = inspect(client)
    for extra in ({"python": "42"}, {"stdin": ""}, {"control": "restart"}):
        result = client.send(requirements={"action": "get"}, **extra)
        assert result.get("isError"), result
    for action in ("get", "reset"):
        for payload in (
            {"r": []},
            {"python": []},
            {"duckdb": []},
            {"python_version": []},
            {"exclude_newer": None},
            {"r": None},
            {"python_version": None},
        ):
            result = client.send(requirements=dict(payload, action=action))
            assert result.get("isError"), result
    result = client.send(requirements={})
    assert result.get("isError"), result
    assert inspect(client) == initial
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_inspection_during_input_and_replacement_resolution(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        environment, started, release = checkpoint_uv_environment(
            Path(directory), "six"
        )
        client = McpClient(binary, execution.serve(), environment)
        try:
            client.initialize_and_list_tools()
            client.send(requirements={"action": "set"})
            old = inspect(client)
            client.send(python='marker = 42; print("before input"); answer = input()')
            assert "[waiting for stdin]" in last_tool_text(client)
            assert inspect(client) == old
            client.send(stdin="kept\n")
            client.send(python="(marker, answer)")
            assert last_tool_text(client) == "(42, 'kept')\n"
            pending = client.start_send(
                control="restart",
                requirements={"action": "set", "python": ["six"]},
                python="import six; six.__name__",
            )
            started.wait("replacement resolver")
            assert inspect(client) == old
            release.release()
            client.receive(pending)
            assert not pending["result"].get("isError"), pending
            assert "'six'" in pending["result"]["content"][0]["text"]
            assert inspect(client)["requirements"] == dict(
                old["requirements"], python=["six"]
            )
            return client.finish()
        finally:
            stop_client(client)
            started.close()
            release.close()


@executions(DIRECT, SANDBOXED)
def test_interrupted_replacement_preserves_worker(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        environment, started, release = checkpoint_uv_environment(temporary, "six")
        interrupted = FifoCheckpoint.create(temporary / "interrupted")
        interrupt_release = FifoCheckpoint.create(temporary / "interrupt-release")
        environment["MCP_CONSOLE_TEST_UV_INTERRUPTED"] = str(interrupted.path)
        environment["MCP_CONSOLE_TEST_UV_INTERRUPT_RELEASE"] = str(
            interrupt_release.path
        )
        client = McpClient(binary, execution.serve(), environment)
        try:
            client.initialize_and_list_tools()
            client.send(
                python="import os; marker = 42; pid = os.getpid()",
                requirements={"action": "set"},
            )
            old = inspect(client)
            pending = client.start_send(
                control="restart",
                requirements={"action": "set", "python": ["six"]},
                python="marker = 0",
            )
            started.wait("replacement resolver")
            assert inspect(client) == old
            interrupt = client.start_send(control="interrupt")
            interrupted.wait("interrupted replacement resolver")
            assert inspect(client) == old
            interrupt_release.release()
            client.receive(pending)
            client.receive(interrupt)
            assert pending["result"].get("isError"), pending
            assert (
                "managed Python resolution failed"
                in pending["result"]["content"][0]["text"]
            ), pending
            assert inspect(client) == old
            client.send(python="marker, os.getpid() == pid")
            assert last_tool_text(client) == "(42, True)\n"
            client.send(control="restart")
            assert inspect(client) == old
            return client.finish()
        finally:
            stop_client(client)
            for checkpoint in (started, release, interrupted, interrupt_release):
                checkpoint.close()


@executions(DIRECT, SANDBOXED)
@requires(command("ir"))
def test_r_duckdb_replacement_failure_and_reset(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        environment, record = recording_fixture_r_environment(
            Path(directory), ("mcpcleared",)
        )
        environment["MCP_CONSOLE_TEST_IR_FAIL_REQUIREMENT"] = "missing.fixture"
        with McpClient(binary, execution.serve(), environment) as client:
            client.initialize_and_list_tools()
            startup = inspect(client)
            assert ir_run_records(record) == []
            client.send(
                r="marker <- 42L; pid <- Sys.getpid()", requirements={"action": "set"}
            )
            empty = inspect(client)
            failure = client.send(
                control="restart",
                requirements={
                    "action": "set",
                    "r": ["missing.fixture"],
                    "python": ["six"],
                },
                r="marker <- 0L",
            )
            assert failure.get("isError"), failure
            assert inspect(client) == empty
            failure = client.send(
                control="restart",
                requirements={
                    "action": "set",
                    "r": ["praise"],
                    "duckdb": ["not_a_real_duckdb_extension"],
                },
            )
            assert failure.get("isError"), failure
            error = failure["content"][0]["text"]
            assert (
                'Failed to download extension "not_a_real_duckdb_extension"' in error
            ), error
            for pattern, replacement in (
                (r'(?<= at URL )"https?://[^"]+"', '"<DuckDB extension URL>"'),
                (
                    r"https://duckdb\.org/docs/stable/extensions/troubleshooting\?\S+",
                    "<DuckDB extension troubleshooting URL>",
                ),
            ):
                error, count = re.subn(pattern, replacement, error, count=1)
                assert count == 1, error
            failure["content"][0]["text"] = error
            assert inspect(client) == empty
            client.send(r="stopifnot(marker == 42L, pid == Sys.getpid())")
            assert last_tool_text(client) == "[done]"
            client.send(
                control="restart",
                requirements={"action": "set", "r": ["praise"], "duckdb": ["fts"]},
                r='stopifnot(requireNamespace("praise"), !exists("marker"))',
            )
            assert not client.transcript[-1]["result"].get("isError"), (
                client.transcript[-1]
            )
            selected = inspect(client)
            assert selected["requirements"] == dict(
                empty["requirements"], r=["praise"], duckdb=["fts"]
            )
            client.send(sql="LOAD fts; SELECT 42 AS answer")
            assert not client.transcript[-1]["result"].get("isError"), (
                client.transcript[-1]
            )
            client.send(
                control="restart", requirements={"action": "set", "python": ["six"]}
            )
            assert inspect(client)["requirements"] == dict(
                empty["requirements"], python=["six"]
            )
            client.send(python="import yaml12; yaml12.__name__")
            assert "py-yaml12" in inspect(client)["requirements"]["python"]
            client.send(r='stopifnot(requireNamespace("mcpcleared", quietly = TRUE))')
            assert "mcpcleared" in inspect(client)["requirements"]["r"]
            client.send(control="restart", requirements={"action": "reset"})
            assert inspect(client)["requirements"] == startup["requirements"]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_large_manifest_round_trip(binary: Path, execution: Execution) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(requirements={"action": "set"})
        packages = [
            f"absent-fixture-{i}; python_version < '0' and platform_system == '{'x' * 120}'"
            for i in range(65)
        ]
        for group in (packages[:64], packages[64:]):
            result = client.send(requirements={"python": group})
            assert not result.get("isError"), result
        result = client.send(requirements={"action": "get"})
        selected = result["structuredContent"]["requirements"]
        assert selected["python"] == sorted(packages)
        assert (
            "complete requirements declaration is in structuredContent"
            in last_tool_text(client)
        )
        result = client.send(requirements=dict(selected, action="set"))
        assert not result.get("isError"), result
        assert (
            client.send(requirements={"action": "get"})["structuredContent"][
                "requirements"
            ]
            == selected
        )
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(command("yamark"))
def test_records_requirement_boundaries(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary, execution.serve(), current_directory=workspace
        ) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"action": "set"}, python="first_environment = 1")
            client.send(
                control="restart",
                requirements={"action": "set", "python": ["six"]},
                python="second_environment = 2",
            )
            result = client.send(
                control="restart",
                requirements={"action": "set", "python_version": [">3", "<2"]},
            )
            assert result.get("isError"), result
            client.send(control="restart", requirements={"action": "reset"})
            transcript = client.finish()
        session = next((workspace / ".agents/console/sessions").iterdir())
        events = [
            json.loads(line)
            for line in (session / "internal/events.jsonl").read_text().splitlines()
        ]
        boundaries = [
            event for event in events if event["event"] == "requirements_selected"
        ]
        assert [event["action"] for event in boundaries] == ["set", "set", "reset"]
        assert boundaries[0]["snapshot"]["requirements"]["python"] == []
        assert boundaries[1]["snapshot"]["requirements"]["python"] == ["six"]
        assert boundaries[2]["snapshot"]["requirements"]["python"] == [
            "numpy",
            "pandas",
        ]
        quarto = (session / "transcript.qmd").read_text()
        assert "execute:\n  eval: false" in quarto
        assert "Recreate the environments at the recorded boundaries" in quarto
        assert quarto.index('"python": []') < quarto.index("first_environment = 1")
        assert quarto.index('"six"') < quarto.index("second_environment = 2")
        markdown = (session / "transcript.md").read_text()
        assert "Requirements selected" in markdown
        formatting = subprocess.run(
            [
                "yamark",
                "format",
                "--diff",
                "--wrap",
                "sentence",
                "--skip-embedded-formatters",
                "transcript.md",
                "transcript.qmd",
            ],
            cwd=session,
            capture_output=True,
            text=True,
        )
        assert formatting.returncode == 0, formatting.stdout + formatting.stderr
        return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
