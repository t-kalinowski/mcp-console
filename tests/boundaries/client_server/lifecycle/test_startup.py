#!/usr/bin/env -S uv run --script

from __future__ import annotations

import json
import os
import select
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import (
    collect_running_output,
    last_tool_text,
    wait_for_evaluation_output,
)
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.native import LOADER_VARIABLE, build_interposer
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, command, requires
from support.resolvers import resolver_fixture_arguments, resolver_fixture_directory
from support.suites import run_this_suite

RUNNING = "\n[running; poll with an empty send]"


@dataclass
class StartupFixture:
    client: McpClient
    root: Path
    started: FifoCheckpoint
    release: FifoCheckpoint
    lifetime: int

    def wait_for_resolver(self) -> None:
        self.started.wait("first-use resolver")

    def wait_for_resolver_exit(self) -> None:
        assert select.select([self.lifetime], [], [], 5)[0], (
            "resolver processes did not exit"
        )
        assert os.read(self.lifetime, 1) == b"", "resolver retained its lifetime pipe"

    def invocations(self) -> list[dict[str, object]]:
        record = self.root / "resolver.jsonl"
        if not record.exists():
            return []
        return [json.loads(line) for line in record.read_text().splitlines()]


@contextmanager
def startup_fixture(
    binary: Path,
    execution: Execution,
    *,
    bootstrap: str = "ir",
    phase: str = "all",
    server_environment: dict[str, str] | None = None,
) -> Iterator[StartupFixture]:
    with resolver_fixture_directory(binary, execution) as temporary:
        fake_bin = temporary / "bin"
        fake_bin.mkdir()
        fixtures = Path(__file__).resolve().parents[3] / "fixtures"
        (fake_bin / bootstrap).symlink_to(fixtures / "startup_ir")
        if bootstrap == "ir":
            # Gate ir while keeping Python preparation on the available uv.
            # Concurrent cases must not bootstrap uv into reticulate's shared cache.
            (fake_bin / "uv").symlink_to(fixtures / "startup_ir")
        (fake_bin / "python3").symlink_to(sys.executable)
        started = FifoCheckpoint.create(temporary / "started")
        release = FifoCheckpoint.create(temporary / "release")
        lifetime = temporary / "lifetime"
        os.mkfifo(lifetime)
        reader = os.open(lifetime, os.O_RDONLY | os.O_NONBLOCK)
        environment, _ = r_test_environment()
        real_ir = shutil.which("ir")
        real_uv = shutil.which("uv")
        assert real_ir is not None and real_uv is not None
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join(
            [str(fake_bin)]
            + [
                entry
                for entry in path.split(os.pathsep)
                if not any((Path(entry) / name).exists() for name in ("ir", "uv"))
            ]
        )
        environment.pop("RETICULATE_UV", None)
        environment.pop("RETICULATE_PYTHON", None)
        environment.update(
            {
                "TMPDIR": str(temporary),
                "MCP_CONSOLE_TEST_REAL_IR": real_ir,
                "MCP_CONSOLE_TEST_REAL_UV": real_uv,
                "MCP_CONSOLE_TEST_STARTUP_PHASE": phase,
                "MCP_CONSOLE_TEST_STARTUP_CLAIM": str(temporary / "gate-claimed"),
                "MCP_CONSOLE_TEST_STARTUP_LIFETIME": str(lifetime),
                "MCP_CONSOLE_TEST_STARTUP_RECORD": str(temporary / "resolver.jsonl"),
                "MCP_CONSOLE_TEST_STARTUP_STARTED": str(started.path),
                "MCP_CONSOLE_TEST_STARTUP_RELEASE": str(release.path),
            }
        )
        environment.update(server_environment or {})
        client = McpClient(
            binary,
            execution.serve(*resolver_fixture_arguments(environment)),
            environment,
            response_timeout=5,
        )
        fixture = StartupFixture(client, temporary, started, release, reader)
        try:
            yield fixture
        finally:
            try:
                client.close()
            finally:
                os.close(reader)
                started.close()
                release.close()


@executions(DIRECT, SANDBOXED)
@requires(command("ir"), command("uv"))
def test_preserves_initialize_buffered_during_startup(
    binary: Path, execution: Execution
) -> Transcript:
    with startup_fixture(binary, execution) as fixture:
        client = fixture.client
        client.initialize_and_list_tools()
        assert fixture.invocations() == [], "initialization started a resolver"
        client.send()
        assert last_tool_text(client) == "\n[idle]"
        client.send(r="must not run", requirements={"r": [""]})
        assert client.transcript[-1]["result"]["isError"] is True
        assert fixture.invocations() == [], "poll or invalid input started a resolver"
        assert not list(fixture.root.glob("sandbox-*"))
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(command("ir"), command("uv"))
def test_initializes_before_uv_bootstrap_installation(
    binary: Path, execution: Execution
) -> Transcript:
    with startup_fixture(binary, execution, bootstrap="uv") as fixture:
        fixture.client.initialize_and_list_tools()
        assert fixture.invocations() == [], "initialization started a resolver"
        return fixture.client.finish()


@executions(DIRECT, SANDBOXED)
@requires(command("ir"), command("uv"))
def test_prepares_python_before_r_bootstrap_validation(
    binary: Path, execution: Execution
) -> Transcript:
    with startup_fixture(binary, execution, phase="discovery") as fixture:
        client = fixture.client
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            cat("ready\n")
            """)
        client.send(r=r, timeout_ms=0)
        assert last_tool_text(client) == RUNNING
        fixture.wait_for_resolver()
        assert fixture.invocations()[-1] == {
            "program": "ir",
            "arguments": ["--version"],
        }
        assert any(
            invocation["program"] == "uv"
            and invocation["arguments"][:2] == ["tool", "run"]
            for invocation in fixture.invocations()
        ), "Python preparation waited for R bootstrap validation"
        fixture.release.release()
        client.response_timeout = 600
        assert collect_running_output(client, "first cell", timeouts_ms=(600_000,)) == (
            "ready\n",
        )
        fixture.wait_for_resolver_exit()
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(command("ir"), command("uv"))
def test_first_cell_prepares_defaults_after_running_response(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with startup_fixture(binary, execution, phase="preparation") as fixture:
        client = fixture.client
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            defaults <- c(
              "tidyverse",
              "reticulate",
              "DBI",
              "duckdb",
              "arrow",
              "nanoarrow"
            )
            managed_index <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") 2L else 1L
            stopifnot(all(defaults %in% list.files(.libPaths()[[managed_index]])))
            stopifnot(identical(reticulate::py_require()$packages, c("numpy", "pandas")))
            cat("scientific defaults ready\n")
            """)
        client.send(r=r, timeout_ms=0)
        assert last_tool_text(client) == RUNNING
        fixture.wait_for_resolver()
        assert not list(fixture.root.glob("sandbox-*"))
        assert any(
            invocation["program"] == "uv"
            and invocation["arguments"][:2] == ["tool", "run"]
            for invocation in fixture.invocations()
        ), "Python defaults waited for the managed R library"
        preparation = fixture.invocations()[-1]["arguments"]
        assert isinstance(preparation, list)
        assert {
            preparation[index + 1]
            for index, argument in enumerate(preparation[:-1])
            if argument == "--with"
        } == {
            "tidyverse",
            "reticulate",
            "DBI",
            "duckdb",
            "arrow",
            "nanoarrow",
            "jsonlite",
            "pillar",
            "tibble",
            "utf8",
        }, preparation
        client.send(timeout_ms=0)
        assert last_tool_text(client) == RUNNING
        fixture.release.release()
        client.response_timeout = 600
        assert collect_running_output(client, "first cell", timeouts_ms=(600_000,)) == (
            "scientific defaults ready\n",
        )
        fixture.wait_for_resolver_exit()
        # fmt: python
        python = code("""
            import numpy, pandas

            print("Python defaults ready")
            """)
        client.send(python=python)
        assert last_tool_text(client) == "Python defaults ready\n"
        sql = code("""
            SELECT extension_name
            FROM duckdb_extensions()
            WHERE installed AND extension_name IN ('icu', 'json')
            ORDER BY extension_name
            """)
        client.send(sql=sql)
        assert {'"icu"', '"json"'} <= set(last_tool_text(client).split()), (
            client.transcript[-1]
        )
        assert (
            len(
                [
                    invocation
                    for invocation in fixture.invocations()
                    if invocation["arguments"][0] == "run"
                ]
            )
            == 1
        ), fixture.invocations()
        assert any(
            invocation["program"] == "uv" for invocation in fixture.invocations()
        ), "Python preparation bypassed the fixture's uv executable"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(command("ir"), command("uv"))
def test_explicit_preparation_keeps_its_wait_precondition(
    binary: Path, execution: Execution
) -> Transcript:
    with startup_fixture(binary, execution, phase="preparation") as fixture:
        client = fixture.client
        client.initialize_and_list_tools()
        preparation = client.start_send(requirements={"r": ["DBI"]}, timeout_ms=0)
        fixture.wait_for_resolver()
        client.request("ping")
        assert "result" not in preparation, (
            "explicit preparation returned before resolution"
        )
        assert not list(fixture.root.glob("sandbox-*"))
        fixture.release.release()
        client.response_timeout = 600
        client.receive(preparation)
        assert preparation["result"] == {
            "content": [{"type": "text", "text": "[prepared]"}],
            "isError": False,
        }
        fixture.wait_for_resolver_exit()
        assert not list(fixture.root.glob("sandbox-*"))
        client.send(r="42L")
        assert last_tool_text(client) == "[1] 42\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(command("ir"), command("uv"))
def test_cancels_resolver_discovery_when_stdin_closes(
    binary: Path, execution: Execution
) -> Transcript:
    with startup_fixture(binary, execution, phase="discovery") as fixture:
        client = fixture.client
        client.initialize_and_list_tools()
        client.send(r="42L", timeout_ms=0)
        assert last_tool_text(client) == RUNNING
        fixture.wait_for_resolver()
        client.stdin.close()
        exit_code = client.process.wait(timeout=5)
        fixture.wait_for_resolver_exit()
        return [
            *client.transcript,
            {
                "exit_code": exit_code,
                "stdout": client.stdout.read(),
                "stderr": client.stderr.read(),
            },
        ]


@executions(DIRECT, SANDBOXED)
@requires(command("ir"), command("uv"))
def test_cancels_default_preparation_when_stdin_closes(
    binary: Path, execution: Execution
) -> Transcript:
    with startup_fixture(binary, execution, phase="preparation") as fixture:
        client = fixture.client
        client.initialize_and_list_tools()
        client.send(r="42L", timeout_ms=0)
        assert last_tool_text(client) == RUNNING
        fixture.wait_for_resolver()
        client.stdin.close()
        exit_code = client.process.wait(timeout=5)
        fixture.wait_for_resolver_exit()
        return [
            *client.transcript,
            {
                "exit_code": exit_code,
                "stdout": client.stdout.read(),
                "stderr": client.stderr.read(),
            },
        ]


@executions(DIRECT, SANDBOXED)
@requires(command("ir"), command("uv"))
def test_interrupts_first_use_preparation_without_running_cell(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with startup_fixture(binary, execution, phase="preparation") as fixture:
        client = fixture.client
        client.initialize_and_list_tools()
        client.send(r="startup_cell_ran <- TRUE", timeout_ms=0)
        assert last_tool_text(client) == RUNNING
        fixture.wait_for_resolver()
        client.send(control="interrupt", timeout_ms=30_000)
        fixture.wait_for_resolver_exit()
        client.response_timeout = 600
        client.send(
            r='exists("startup_cell_ran", inherits = FALSE)', timeout_ms=600_000
        )
        assert last_tool_text(client) == "[1] FALSE\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES, command("ir"), command("uv"))
def test_restart_replaces_first_use_cell_and_stdin(
    binary: Path, execution: Execution
) -> Transcript:
    with ExitStack() as resources:
        root = Path(resources.enter_context(tempfile.TemporaryDirectory()))
        contended = FifoCheckpoint.create(root / "contended")
        cancel_release = FifoCheckpoint.create(root / "cancel-release")
        unlocked = FifoCheckpoint.create(root / "unlocked")
        release = FifoCheckpoint.create(root / "release")
        parked = FifoCheckpoint.create(root / "parked")
        for checkpoint in (contended, cancel_release, unlocked, release, parked):
            resources.callback(checkpoint.close)
        armed = root / "armed"
        environment = {
            LOADER_VARIABLE: str(
                build_interposer(root, "evaluation_return_interposer")
            ),
            "MCP_CONSOLE_TEST_COMPLETION_ARMED": str(armed),
            "MCP_CONSOLE_TEST_COMPLETION_CONTENDED": str(contended.path),
            "MCP_CONSOLE_TEST_COMPLETION_CANCEL_RELEASE": str(cancel_release.path),
            "MCP_CONSOLE_TEST_COMPLETION_UNLOCKED": str(unlocked.path),
            "MCP_CONSOLE_TEST_COMPLETION_RELEASE": str(release.path),
            "MCP_CONSOLE_TEST_COMPLETION_PARKED": str(parked.path),
        }
        fixture = resources.enter_context(
            startup_fixture(
                binary, execution, phase="preparation", server_environment=environment
            )
        )
        resources.callback(release.release)
        resources.callback(cancel_release.release)
        client = fixture.client
        client.initialize_and_list_tools()
        client.send(python="startup_cell_ran = True", stdin="old input\n", timeout_ms=0)
        assert last_tool_text(client) == RUNNING
        fixture.wait_for_resolver()
        armed.touch()
        client.response_timeout = 600
        # fmt: python
        python = code("""
            assert "startup_cell_ran" not in globals()
            assert input() == "replacement input"
            print("replacement only")
            """)
        # Withhold the newline so managed input must report waiting before
        # the replacement can complete, regardless of the input exposure grace.
        replacement = client.start_send(
            control="restart",
            python=python,
            stdin="replacement input",
            timeout_ms=600_000,
        )
        contended.wait("restart waits for the cancelling evaluation's worker lock")
        cancel_release.release()
        unlocked.wait("old evaluation released the worker lock")
        client.receive(replacement)
        assert last_tool_text(client) == code("""
            [active evaluation stopped by session restart request]
            [starting new worker]
            [input requested: ""]
            [waiting for stdin]
            """).removesuffix("\n")
        wait_for_evaluation_output(
            client,
            "replacement only\n[done]",
            "replacement managed input",
            stdin="\n",
        )
        assert last_tool_text(client).count("replacement only\n") == 1, (
            client.transcript[-1]
        )
        fixture.wait_for_resolver_exit()
        release.release()
        parked.wait("old evaluation task returned to the pool")
        client.send()
        assert last_tool_text(client) == "\n[idle]"
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
