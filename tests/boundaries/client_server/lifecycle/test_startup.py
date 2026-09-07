#!/usr/bin/env -S uv run --script

from __future__ import annotations

import json
import os
import select
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import collect_running_output, last_tool_text
from support.events import Events
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.processes import (
    ProcessIdentity,
    capture_process_identity,
    kill_processes,
)
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.suites import run_this_suite

PLATFORMS = {"darwin", "linux"}
REQUIRED_COMMANDS = {"ir", "uv"}
RUNNING = "\n[running; poll with an empty send]"


@dataclass
class StartupFixture:
    client: McpClient
    root: Path
    started: FifoCheckpoint
    release: FifoCheckpoint
    exits: Events
    identities: list[ProcessIdentity] = field(default_factory=list)

    def wait_for_resolver(self) -> None:
        self.started.wait("first-use resolver")
        identity = self.root / "identity"
        pids = set(map(int, identity.read_text(encoding="utf-8").split()))
        assert len(pids) == 2, pids
        self.identities = [capture_process_identity(pid) for pid in pids]
        for pid in pids:
            self.exits.watch_process(pid)

    def wait_for_resolver_exit(self) -> None:
        pending = {identity[0] for identity in self.identities}
        deadline = time.monotonic() + 5
        while pending:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"resolver processes did not exit: {pending}"
            observed = self.exits.wait(remaining)
            assert observed, f"resolver processes did not exit: {pending}"
            pending.difference_update(observed)

    def invocations(self) -> list[dict[str, object]]:
        record = self.root / "resolver.jsonl"
        if not record.exists():
            return []
        return [json.loads(line) for line in record.read_text().splitlines()]


@contextmanager
def startup_fixture(
    binary: Path, *, bootstrap: str = "ir", phase: str = "all"
) -> Iterator[StartupFixture]:
    with tempfile.TemporaryDirectory() as directory, Events() as exits:
        temporary = Path(directory)
        fake_bin = temporary / "bin"
        fake_bin.mkdir()
        fixtures = Path(__file__).resolve().parents[3] / "fixtures"
        (fake_bin / bootstrap).symlink_to(fixtures / "startup_ir")
        (fake_bin / "python3").symlink_to(sys.executable)
        started = FifoCheckpoint.create(temporary / "started")
        release = FifoCheckpoint.create(temporary / "release")
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
                "MCP_CONSOLE_TEST_STARTUP_IDENTITY": str(temporary / "identity"),
                "MCP_CONSOLE_TEST_STARTUP_RECORD": str(temporary / "resolver.jsonl"),
                "MCP_CONSOLE_TEST_STARTUP_STARTED": str(started.path),
                "MCP_CONSOLE_TEST_STARTUP_RELEASE": str(release.path),
            }
        )
        client = McpClient(
            binary,
            ("serve",),
            environment,
            response_timeout=5,
        )
        fixture = StartupFixture(client, temporary, started, release, exits)
        try:
            yield fixture
        finally:
            # An early handshake failure can leave the resolver gated before
            # the test gets to observe it. Capture its identity before cleanup.
            if (
                not fixture.identities
                and select.select([started.descriptor], [], [], 0)[0]
            ):
                fixture.wait_for_resolver()
            try:
                client.close()
            finally:
                kill_processes(fixture.identities)
                started.close()
                release.close()


def test_preserves_initialize_buffered_during_startup(binary: Path) -> Transcript:
    with startup_fixture(binary) as fixture:
        client = fixture.client
        client.initialize_and_list_tools()
        assert fixture.invocations() == [], "initialization started a resolver"
        client.send()
        assert last_tool_text(client) == "\n[idle]"
        client.send(r="must not run", requirements={"r": [""]})
        assert client.transcript[-1]["result"]["isError"] is True
        assert fixture.invocations() == [], "poll or invalid input started a resolver"
        assert not list(fixture.root.glob("mcp-console-tmp-*"))
        return client.finish()


def test_initializes_before_uv_bootstrap_installation(binary: Path) -> Transcript:
    with startup_fixture(binary, bootstrap="uv") as fixture:
        fixture.client.initialize_and_list_tools()
        assert fixture.invocations() == [], "initialization started a resolver"
        return fixture.client.finish()


def test_first_cell_prepares_defaults_after_running_response(
    binary: Path,
) -> Transcript:
    with startup_fixture(binary, phase="preparation") as fixture:
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
            stopifnot(all(defaults %in% list.files(.libPaths()[[1L]])))
            stopifnot(identical(reticulate::py_require()$packages, c("numpy", "pandas")))
            cat("scientific defaults ready\n")
            """)
        client.send(r=r, timeout_ms=0)
        assert last_tool_text(client) == RUNNING
        fixture.wait_for_resolver()
        assert not list(fixture.root.glob("mcp-console-tmp-*"))
        preparation = fixture.invocations()[-1]["arguments"]
        assert isinstance(preparation, list)
        assert {
            preparation[index + 1]
            for index, argument in enumerate(preparation[:-1])
            if argument == "--with"
        } == {
            "tidyverse",
            "github::rstudio/reticulate",
            "DBI",
            "duckdb",
            "arrow",
            "nanoarrow",
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
        return client.finish()


def test_explicit_preparation_keeps_its_wait_precondition(binary: Path) -> Transcript:
    with startup_fixture(binary, phase="preparation") as fixture:
        client = fixture.client
        client.initialize_and_list_tools()
        preparation = client.start_send(requirements={"r": ["DBI"]}, timeout_ms=0)
        fixture.wait_for_resolver()
        client.request("ping")
        assert "result" not in preparation, (
            "explicit preparation returned before resolution"
        )
        assert not list(fixture.root.glob("mcp-console-tmp-*"))
        fixture.release.release()
        client.response_timeout = 600
        client.receive(preparation)
        assert preparation["result"] == {
            "content": [{"type": "text", "text": "[prepared]"}],
            "isError": False,
        }
        fixture.wait_for_resolver_exit()
        assert not list(fixture.root.glob("mcp-console-tmp-*"))
        client.send(r="42L")
        assert last_tool_text(client) == "[1] 42\n"
        return client.finish()


def test_cancels_resolver_discovery_when_stdin_closes(binary: Path) -> Transcript:
    with startup_fixture(binary, phase="discovery") as fixture:
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


def test_cancels_default_preparation_when_stdin_closes(binary: Path) -> Transcript:
    with startup_fixture(binary, phase="preparation") as fixture:
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


def test_interrupts_first_use_preparation_without_running_cell(
    binary: Path,
) -> Transcript:
    with startup_fixture(binary, phase="preparation") as fixture:
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


def test_restart_replaces_first_use_cell_and_stdin(binary: Path) -> Transcript:
    with startup_fixture(binary, phase="preparation") as fixture:
        client = fixture.client
        client.initialize_and_list_tools()
        client.send(python="startup_cell_ran = True", stdin="old input\n", timeout_ms=0)
        assert last_tool_text(client) == RUNNING
        fixture.wait_for_resolver()
        client.response_timeout = 600
        # fmt: python
        python = code("""
            assert "startup_cell_ran" not in globals()
            assert input() == "replacement input"
            print("replacement only")
            """)
        client.send(
            control="restart",
            python=python,
            stdin="replacement input\n",
            timeout_ms=600_000,
        )
        assert last_tool_text(client).count("replacement only\n") == 1, (
            client.transcript[-1]
        )
        fixture.wait_for_resolver_exit()
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
