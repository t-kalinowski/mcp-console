#!/usr/bin/env -S uv run --script

from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.checkpoints import FifoCheckpoint
from support.client import McpClient, stop_client
from support.macos import (
    DarwinProcessIdentity,
    capture_darwin_process_identity,
    darwin_child_process_identities,
    kill_darwin_processes,
    live_darwin_processes,
)
from support.records import Transcript
from support.suites import run_this_suite

PLATFORMS = {"darwin"}
TIMEOUT = 10
MARKER_NAME = "mcp-console-startup-marker"


def _build_manager_start_interposer(directory: Path) -> Path:
    source = directory / "manager-start-interposer.c"
    library = directory / "manager-start-interposer.dylib"
    fixture = (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "native"
        / "manager_start_interposer.c"
    )
    shutil.copyfile(fixture, source)
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Werror",
            "-dynamiclib",
            "-o",
            library,
            source,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return library


def _worker_generation_processes(server_pid: int) -> tuple[int, int]:
    deadline = time.monotonic() + TIMEOUT
    while True:
        processes = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        ).stdout
        records = []
        for process in processes.splitlines():
            fields = process.strip().split(maxsplit=2)
            if len(fields) == 3:
                records.append((int(fields[0]), int(fields[1]), fields[2]))

        # The CLI launcher is the sandbox owner. Locate its gated root and
        # manager by ancestry and executable role.
        descendants = {server_pid}
        while True:
            discovered = {pid for pid, parent, _ in records if parent in descendants}
            if discovered.issubset(descendants):
                break
            descendants.update(discovered)

        managers = [
            pid
            for pid, _, command in records
            if pid in descendants and "sandbox-manager" in command.split()
        ]
        roots = [
            pid
            for pid, _, command in records
            if pid in descendants and "sandbox-target" in command.split()
        ]
        assert len(managers) <= 1, managers
        assert len(roots) <= 1, roots
        if managers and roots:
            return roots[0], managers[0]
        assert time.monotonic() < deadline, (
            "worker generation did not start its root and manager"
        )
        time.sleep(0.01)


def _wait_for_startup_cleanup(
    identities: tuple[DarwinProcessIdentity, ...],
) -> None:
    deadline = time.monotonic() + TIMEOUT
    while True:
        survivors = live_darwin_processes(identities)
        if not survivors:
            return
        assert time.monotonic() < deadline, (
            f"startup cleanup left processes {survivors}"
        )
        time.sleep(0.01)


def _assert_zod_echo(entry: dict[str, object]) -> None:
    result = entry["result"]
    assert result == {
        "content": [{"type": "text", "text": "zod: echo\n"}],
        "isError": False,
    }, result


def test_sandbox_setup_failure_is_reported_and_retryable(binary: Path) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as directory:
        temporary_parent = Path(directory) / "sandbox-parent"
        temporary_parent.write_text("not a directory", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = str(temporary_parent)
        client = McpClient(binary, ("serve", "--worker", str(worker)), environment)
        try:
            server = capture_darwin_process_identity(client.process.pid)
            client._initialize_and_list_tools()
            result = client.send(r="echo echo")
            assert result == {
                "content": [
                    {"type": "text", "text": "[worker relay exited before readiness]"}
                ],
                "isError": True,
            }, result
            assert darwin_child_process_identities(server) == ()

            temporary_parent.unlink()
            temporary_parent.mkdir()
            client.send(r="echo echo")
            _assert_zod_echo(client.transcript[-1])
            transcript, stderr = client._finish_with_standard_error()
            diagnostic = re.sub(
                re.escape(str(temporary_parent)) + r"/mcp-console-tmp-\d+-\d+",
                "<sandbox temp>",
                stderr,
            )
            assert diagnostic == (
                "failed to create temporary directory `<sandbox temp>`: "
                "Not a directory (os error 20)\n"
            ), diagnostic
            transcript.append({"stderr": diagnostic})
            return transcript
        finally:
            stop_client(client)


def test_manager_failure_before_readiness_keeps_custom_relay_gated(
    binary: Path,
) -> Transcript:
    fixture_root = Path(__file__).resolve().parents[3] / "fixtures"
    worker = fixture_root / "zod"
    marker_relay = fixture_root / "startup_marker_relay"

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        manager_started = FifoCheckpoint.create(temporary / "manager-started")
        manager_release = FifoCheckpoint.create(temporary / "manager-release")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_BINARY"] = str(binary)
        environment["MCP_CONSOLE_TEST_MANAGER_START"] = str(manager_started.path)
        environment["MCP_CONSOLE_TEST_MANAGER_RELEASE"] = str(manager_release.path)
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_manager_start_interposer(temporary)
        )

        client = McpClient(
            binary,
            (
                "serve",
                "--worker",
                str(worker),
                "--relay",
                str(marker_relay),
            ),
            environment,
        )
        identities: tuple[DarwinProcessIdentity, ...] = ()
        replacement_released = False
        try:
            client._initialize_and_list_tools()
            waiting = client._start_send(r="echo echo")
            manager_started.wait("manager startup")

            root_pid, manager_pid = _worker_generation_processes(client.process.pid)
            root = capture_darwin_process_identity(root_pid)
            manager = capture_darwin_process_identity(manager_pid)
            identities = (root, manager)
            assert list(temporary.glob(f"**/{MARKER_NAME}")) == []

            assert kill_darwin_processes((manager,)) == [manager_pid], (
                "sandbox manager exited before failure injection"
            )
            readable, _, _ = select.select([client.stdout], [], [], TIMEOUT)
            assert readable, "server did not return after sandbox manager failure"
            client._receive(waiting)
            result = waiting["result"]
            assert result == {
                "content": [
                    {
                        "type": "text",
                        "text": "[worker relay exited before readiness]",
                    }
                ],
                "isError": True,
            }, result
            readable, _, _ = select.select([client.stderr], [], [], TIMEOUT)
            assert readable, "sandbox launcher did not report its startup failure"
            diagnostic = client.stderr.readline().rstrip("\n")
            assert diagnostic == (
                "sandbox manager did not become ready: failed to fill whole buffer"
            ), diagnostic
            _wait_for_startup_cleanup(identities)
            assert list(temporary.glob(f"**/{MARKER_NAME}")) == []
            waiting["startup_supervision_failure"] = {
                "manager": "killed before readiness",
                "custom_relay": "did not execute",
                "sandbox_stderr": diagnostic,
                "verified_cleanup": "gated relay root and manager",
            }

            replacement = client._start_send(r="echo echo")
            manager_started.wait("replacement manager startup")
            manager_release.release()
            replacement_released = True
            client._receive(replacement)
            _assert_zod_echo(replacement)
            markers = list(temporary.glob(f"**/{MARKER_NAME}"))
            assert len(markers) == 1, markers
            replacement["startup_supervision_recovery"] = {
                "custom_relay": "executed only for the replacement generation",
            }
            return client._finish()
        finally:
            if not replacement_released:
                manager_release.release()
            stop_client(client)
            if identities:
                kill_darwin_processes(identities)
            manager_started.close()
            manager_release.close()


if __name__ == "__main__":
    run_this_suite(__file__)
