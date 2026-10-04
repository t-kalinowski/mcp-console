#!/usr/bin/env -S uv run --script

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, command, requires
from support.resolvers import resolver_fixture_arguments, resolver_fixture_directory
from boundaries.client_server._harness import interrupt_paused_preparation
from support.suites import run_this_suite

RUNNING = "\n[running; poll with an empty send]"


@contextmanager
def before_resolver_spawn(
    binary: Path, execution: Execution, ordinal: int
) -> Iterator[tuple[McpClient, FifoCheckpoint, FifoCheckpoint, Path]]:
    with ExitStack() as resources:
        root = resources.enter_context(resolver_fixture_directory(binary, execution))
        started = FifoCheckpoint.create(root / "spawn-started")
        release = FifoCheckpoint.create(root / "spawn-release")
        resources.callback(started.close)
        resources.callback(release.close)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        fixtures = Path(__file__).resolve().parents[3] / "fixtures"
        (fake_bin / "ir").symlink_to(fixtures / "startup_ir")
        (fake_bin / "uv").symlink_to(fixtures / "startup_ir")
        environment, _ = r_test_environment()
        real_ir = shutil.which("ir")
        real_uv = shutil.which("uv")
        assert real_ir is not None and real_uv is not None
        environment.update(
            {
                "TMPDIR": str(root),
                "PATH": os.pathsep.join([str(fake_bin), environment["PATH"]]),
                "MCP_CONSOLE_TEST_REAL_IR": real_ir,
                "MCP_CONSOLE_TEST_REAL_UV": real_uv,
                "MCP_CONSOLE_TEST_STARTUP_PHASE": "none",
                "MCP_CONSOLE_TEST_STARTUP_RECORD": str(root / "resolver.jsonl"),
                "MCP_CONSOLE_TEST_SPAWN_LIBRARY": str(
                    build_interposer(root, "resolver_spawn_interposer")
                ),
                "MCP_CONSOLE_TEST_SPAWN_WORKLOAD": "1",
                "MCP_CONSOLE_TEST_SPAWN_ARMED": str(root / "armed"),
                "MCP_CONSOLE_TEST_SPAWN_ORDINAL": str(ordinal),
                "MCP_CONSOLE_TEST_SPAWN_STARTED": str(started.path),
                "MCP_CONSOLE_TEST_SPAWN_RELEASE": str(release.path),
            }
        )
        (root / "armed").touch()
        client = resources.enter_context(
            McpClient(
                binary,
                execution.serve(
                    *resolver_fixture_arguments(environment),
                    "-c",
                    "resolver.environment."
                    + LOADER_VARIABLE
                    + "="
                    + json.dumps(environment["MCP_CONSOLE_TEST_SPAWN_LIBRARY"]),
                ),
                environment,
                response_timeout=5,
            )
        )
        try:
            client.initialize_and_list_tools()
            yield client, started, release, root
        finally:
            # Release the workload before transport teardown, even
            # when an assertion fails while it is paused before child creation.
            release.release()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, NATIVE_FIXTURES, command("ir"), command("uv"))
def test_interrupts_first_cell_before_resolver_spawn(
    binary: Path, execution: Execution
) -> Transcript:
    with before_resolver_spawn(binary, execution, 1) as (
        client,
        started,
        release,
        root,
    ):
        client.send(r="startup_cell_ran <- TRUE", timeout_ms=0)
        assert last_tool_text(client) == RUNNING
        started.wait("first resolver has not been spawned")
        assert not (root / "resolver.jsonl").exists()
        interrupt_paused_preparation(client, release)
        client.response_timeout = 600
        client.send(timeout_ms=600_000)
        client.send(
            r='exists("startup_cell_ran", inherits = FALSE)', timeout_ms=600_000
        )
        assert last_tool_text(client) == "[1] FALSE\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, NATIVE_FIXTURES, command("ir"), command("uv"))
def test_interrupts_first_cell_between_resolver_phases(
    binary: Path, execution: Execution
) -> Transcript:
    with before_resolver_spawn(binary, execution, 2) as (
        client,
        started,
        release,
        root,
    ):
        client.send(r="startup_cell_ran <- TRUE", timeout_ms=0)
        assert last_tool_text(client) == RUNNING
        started.wait("default preparation has not been spawned")
        invocations = [
            json.loads(line)
            for line in (root / "resolver.jsonl").read_text().splitlines()
        ]
        assert len(invocations) == 1, invocations
        assert invocations[0]["program"] == "uv", invocations
        assert invocations[0]["arguments"][:2] == ["python", "list"], invocations
        interrupt_paused_preparation(client, release)
        client.response_timeout = 600
        client.send(timeout_ms=600_000)
        client.send(
            r='exists("startup_cell_ran", inherits = FALSE)', timeout_ms=600_000
        )
        assert last_tool_text(client) == "[1] FALSE\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, NATIVE_FIXTURES, command("ir"), command("uv"))
def test_interrupts_first_cell_admitted_during_stdin_startup(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with before_resolver_spawn(binary, execution, 1) as (
        client,
        started,
        release,
        root,
    ):
        client.send(stdin="old input\n", timeout_ms=0)
        assert last_tool_text(client) == "[worker starting]"
        started.wait("stdin startup has not spawned its first resolver")
        assert not (root / "resolver.jsonl").exists()
        client.send(r="startup_cell_ran <- TRUE", timeout_ms=0)
        assert last_tool_text(client) == RUNNING
        interrupt_paused_preparation(client, release)
        client.response_timeout = 600
        client.send(timeout_ms=600_000)
        client.send(
            r='exists("startup_cell_ran", inherits = FALSE)', timeout_ms=600_000
        )
        assert client.transcript[-1]["result"].get("isError") is not True, [
            entry for entry in client.transcript if "send" in entry
        ]
        assert last_tool_text(client) == "[1] FALSE\n"
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
