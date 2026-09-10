#!/usr/bin/env -S uv run --script

import os
import select
import time
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.checkpoints import FifoCheckpoint
from support.native import build_interposer
from support.client import McpClient, stop_client
from support.execution import SANDBOXED
from support.macos import (
    DarwinProcessIdentity,
    kill_darwin_processes,
    live_darwin_processes,
    capture_darwin_process_identity,
    darwin_child_process_identities,
)
from support.records import Transcript
from support.requirements import (
    MACOS_SANDBOX,
    NATIVE_FIXTURES,
    PROCESS_EVENTS,
    requires,
)
from support.suites import run_this_suite


TIMEOUT = 10
MARKER_NAME = "mcp-console-startup-marker"


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


@requires(MACOS_SANDBOX, PROCESS_EVENTS)
def test_sandbox_setup_failure_is_reported_and_retryable(binary: Path) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as directory:
        temporary_parent = Path(directory) / "sandbox-parent"
        temporary_parent.write_text("not a directory", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = str(temporary_parent)
        client = McpClient(
            binary, SANDBOXED.serve("--worker", str(worker)), environment
        )
        try:
            server = capture_darwin_process_identity(client.process.pid)
            client.initialize_and_list_tools()
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
            transcript, stderr = client.finish_with_standard_error()
            assert stderr == (
                "mcp-console-sandbox: create private storage: "
                "Not a directory (os error 20)\n"
            ), stderr
            transcript.append({"stderr": stderr})
            return transcript
        finally:
            stop_client(client)


@requires(MACOS_SANDBOX, PROCESS_EVENTS, NATIVE_FIXTURES)
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
            build_interposer(temporary, "manager_start_interposer")
        )

        client = McpClient(
            binary,
            SANDBOXED.serve("--worker", str(worker), "--relay", str(marker_relay)),
            environment,
        )
        identities: tuple[DarwinProcessIdentity, ...] = ()
        replacement_released = False
        try:
            client.initialize_and_list_tools()
            waiting = client.start_send(r="echo echo")
            manager_started.wait("manager startup")

            (manager,) = darwin_child_process_identities(
                capture_darwin_process_identity(client.process.pid)
            )
            (root,) = darwin_child_process_identities(manager)
            manager_pid = manager[0]
            identities = (root, manager)
            assert list(temporary.glob(f"**/{MARKER_NAME}")) == []

            assert kill_darwin_processes((manager,)) == [manager_pid], (
                "sandbox manager exited before failure injection"
            )
            readable, _, _ = select.select([client.stdout], [], [], TIMEOUT)
            assert readable, "server did not return after sandbox manager failure"
            client.receive(waiting)
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
            diagnostic = client.stderr.readline(timeout=TIMEOUT).rstrip("\n")
            assert diagnostic == ("mcp-console-sandbox: failed to fill whole buffer"), (
                diagnostic
            )
            _wait_for_startup_cleanup(identities)
            assert list(temporary.glob(f"**/{MARKER_NAME}")) == []
            waiting["startup_supervision_failure"] = {
                "manager": "killed before readiness",
                "custom_relay": "did not execute",
                "sandbox_stderr": diagnostic,
                "verified_cleanup": "gated relay root and manager",
            }

            replacement = client.start_send(r="echo echo")
            manager_started.wait("replacement manager startup")
            manager_release.release()
            replacement_released = True
            client.receive(replacement)
            _assert_zod_echo(replacement)
            markers = list(temporary.glob(f"**/{MARKER_NAME}"))
            assert len(markers) == 1, markers
            replacement["startup_supervision_recovery"] = {
                "custom_relay": "executed only for the replacement generation",
            }
            return client.finish()
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
