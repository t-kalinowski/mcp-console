#!/usr/bin/env -S uv run --script

from __future__ import annotations

import os
import select
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.checkpoints import FifoCheckpoint
from support.client import McpClient, stop_client
from support.macos import (
    DarwinProcessIdentity,
    capture_darwin_process_identity,
    kill_darwin_processes,
)
from support.records import Transcript
from support.suites import run_this_suite

PLATFORMS = {"darwin"}
REQUIRED_COMMANDS = {"ir"}


@contextmanager
def blocked_startup(
    binary: Path, phase: str
) -> Iterator[tuple[McpClient, FifoCheckpoint, select.kqueue, set[int]]]:
    with tempfile.TemporaryDirectory() as directory, closing(select.kqueue()) as exits:
        temporary = Path(directory)
        fake_bin = temporary / "bin"
        fake_bin.mkdir()
        fixture = Path(__file__).resolve().parents[3] / "fixtures" / "startup_ir"
        (fake_bin / "ir").symlink_to(fixture)
        started = FifoCheckpoint.create(temporary / "started")
        release = FifoCheckpoint.create(temporary / "release")
        identity = temporary / "identity"
        environment = os.environ.copy()
        real_ir = shutil.which("ir")
        assert real_ir is not None, "real ir is required"
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment.update(
            {
                "PATH": os.pathsep.join((str(fake_bin), path)),
                "MCP_CONSOLE_TEST_REAL_IR": real_ir,
                "MCP_CONSOLE_TEST_STARTUP_PHASE": phase,
                "MCP_CONSOLE_TEST_STARTUP_IDENTITY": str(identity),
                "MCP_CONSOLE_TEST_STARTUP_STARTED": str(started.path),
                "MCP_CONSOLE_TEST_STARTUP_RELEASE": str(release.path),
            }
        )
        client = McpClient(binary, ("serve",), environment)
        identities: list[DarwinProcessIdentity] = []
        try:
            started.wait(f"startup resolver {phase}")
            pids = set(map(int, identity.read_text(encoding="utf-8").split()))
            assert len(pids) == 2, pids
            identities = [capture_darwin_process_identity(pid) for pid in pids]
            watches = [
                select.kevent(
                    pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                    fflags=select.KQ_NOTE_EXIT,
                )
                for pid in pids
            ]
            assert exits.control(watches, 0, 0) == []
            yield client, release, exits, pids
        finally:
            kill_darwin_processes(identities)
            stop_client(client)
            started.close()
            release.close()


def wait_for_resolver_exit(exits: select.kqueue, pids: set[int]) -> None:
    pending = pids.copy()
    deadline = time.monotonic() + 5
    while pending:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"startup resolver processes did not exit: {pending}"
        observed = exits.control(None, len(pending), remaining)
        assert observed, f"startup resolver processes did not exit: {pending}"
        for event in observed:
            assert event.filter == select.KQ_FILTER_PROC, event
            assert event.fflags & select.KQ_NOTE_EXIT, event
            pending.remove(event.ident)


def test_cancels_resolver_discovery_when_stdin_closes(binary: Path) -> Transcript:
    with blocked_startup(binary, "discovery") as (client, _release, exits, pids):
        client.start_request(
            "initialize",
            protocolVersion="2025-11-25",
            capabilities={},
            clientInfo={"name": "acceptance-test", "version": "1.0.0"},
        )
        client.stdin.close()
        exit_code = client.process.wait(timeout=5)
        wait_for_resolver_exit(exits, pids)
        return [
            *client.transcript,
            {
                "exit_code": exit_code,
                "stdout": client.stdout.read(),
                "stderr": client.stderr.read(),
            },
        ]


def test_cancels_default_preparation_when_stdin_closes(binary: Path) -> Transcript:
    with blocked_startup(binary, "preparation") as (client, _release, exits, pids):
        client.start_request(
            "initialize",
            protocolVersion="2025-11-25",
            capabilities={},
            clientInfo={"name": "acceptance-test", "version": "1.0.0"},
        )
        client.stdin.close()
        exit_code = client.process.wait(timeout=5)
        wait_for_resolver_exit(exits, pids)
        return [
            *client.transcript,
            {
                "exit_code": exit_code,
                "stdout": client.stdout.read(),
                "stderr": client.stderr.read(),
            },
        ]


def test_preserves_initialize_buffered_during_startup(binary: Path) -> Transcript:
    with blocked_startup(binary, "discovery") as (client, release, _exits, _pids):
        initialize = client.start_request(
            "initialize",
            protocolVersion="2025-11-25",
            capabilities={},
            clientInfo={"name": "acceptance-test", "version": "1.0.0"},
        )
        release.release()
        readable, _, _ = select.select([client.stdout], [], [], 30)
        assert readable, "server did not answer initialize after startup"
        client.receive(initialize)
        client.notify("notifications/initialized")
        client.request("tools/list")
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
