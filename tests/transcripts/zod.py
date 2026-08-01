#!/usr/bin/env -S uv run --script

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from _support import McpClient, Transcript, run_this_suite


PLATFORMS = {"darwin"}


def test_routes_send_over_sideband(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client.initialize_and_list_tools()
    client.call_tool("send", r="hello")
    return client.finish()


def test_restarts_after_unexpected_sideband_message(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client.initialize_and_list_tools()
            failed_call = client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "send",
                        "arguments": {"r": "violate protocol"},
                    },
                }
            )
            group_marker = wait_for_marker(
                temporary_path,
                "zod-process-group",
                client,
            )
            worker_group = read_worker_group(group_marker)
            client.receive(failed_call)
            assert not process_group_exists(worker_group), "Zod outlived its failure"

            restarted_call = client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "send",
                        "arguments": {"r": "hello"},
                    },
                }
            )
            client.receive(restarted_call)
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_restarts_after_worker_exit(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client.initialize_and_list_tools()
    client.call_tool("send", r="exit unexpectedly")
    assert client.transcript[-1]["output"]["result"]["isError"] is True
    client.call_tool("send", r="hello")
    return client.finish()


def test_runs_worker_inside_sandbox(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        host_file = Path(temporary_directory) / "host.txt"
        host_file.write_text("host data", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_SANDBOX_PROBE_PATH"] = str(host_file)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        client.call_tool("send", r="probe sandbox")
        transcript = client.finish()

        assert host_file.read_text(encoding="utf-8") == "host data"
        return transcript


def test_shuts_down_stalled_worker(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        environment = os.environ.copy()
        temporary_path = Path(temporary_directory)
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client.initialize_and_list_tools()
            client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "send",
                        "arguments": {"r": "stall"},
                    },
                }
            )
            group_marker = wait_for_marker(temporary_path, "zod-process-group", client)
            worker_group = read_worker_group(group_marker)
            wait_for_marker(temporary_path, "zod-stalled", client)
            client.stdin.close()
            try:
                return_code = client.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    "mcp-console did not stop its stalled worker"
                ) from None

            assert return_code == 0, client.stderr.read()
            client.stdout.read()
            assert client.stderr.read() == ""
            assert not process_group_exists(worker_group), "Zod outlived mcp-console"
            passed = True
            return client.transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_shutdown_deadline_does_not_wait_for_sideband_writer(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_BLOCK_SIDEBAND_WRITE"] = "1"
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client.initialize_and_list_tools()
            entry = client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "send",
                        "arguments": {"r": "x" * (2 * 1024 * 1024)},
                    },
                }
            )
            group_marker = wait_for_marker(
                temporary_path,
                "zod-process-group",
                client,
            )
            worker_group = read_worker_group(group_marker)
            wait_for_marker(
                temporary_path,
                "zod-sideband-blocked",
                client,
            )
            entry["input"]["params"]["arguments"]["r"] = "<large cell>"
            shutdown_started = time.monotonic()
            client.stdin.close()
            try:
                return_code = client.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    "mcp-console did not enforce its worker shutdown deadline"
                ) from None
            shutdown_elapsed = time.monotonic() - shutdown_started

            assert shutdown_elapsed < 1.5, (
                f"worker shutdown took {shutdown_elapsed:.3f} seconds"
            )
            assert return_code == 0, client.stderr.read()
            client.stdout.read()
            assert client.stderr.read() == ""
            assert not process_group_exists(worker_group), "Zod outlived mcp-console"
            passed = True
            return client.transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def wait_for_marker(root: Path, name: str, client: McpClient) -> Path:
    deadline = time.monotonic() + 3
    while True:
        markers = list(root.glob(f"**/{name}"))
        if markers:
            assert len(markers) == 1, f"found multiple {name} markers"
            return markers[0]
        assert client.process.poll() is None, "mcp-console stopped before Zod stalled"
        assert time.monotonic() < deadline, "Zod did not report its stall checkpoint"
        time.sleep(0.01)


def read_worker_group(marker: Path) -> int:
    worker_group = int(marker.read_text(encoding="utf-8"))
    assert worker_group != os.getpgrp(), "Zod did not enter a dedicated process group"
    return worker_group


def process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    return True


def stop_process_group(process_group: int | None) -> None:
    if process_group is None:
        return
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


if __name__ == "__main__":
    run_this_suite(__file__)
