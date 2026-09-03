#!/usr/bin/env -S uv run --script

import array
import base64
import fcntl
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import termios
import time
from datetime import datetime
from pathlib import Path
from typing import Self

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import (
    FifoCheckpoint,
    McpClient,
    Transcript,
    TranscriptWithCompanions,
    build_manager_interposer,
    code,
    r_test_environment,
    run_this_suite,
    stop_client,
)

PLATFORMS = {"darwin"}
LARGE_OUTPUT_SIZE = 2 * 1024 * 1024
PENDING_TEXT_BUDGET = 8 * 1024 * 1024
TEST_GATED_RESPONSE_SIZE = 128 * 1024
FIXTURE_CHECKPOINT_TIMEOUT_SECONDS = 15
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)
TEST_EVENT_FIFO_NAME = "zod-test-events"
TEST_CONTROL_FIFO_NAME = "zod-test-control"
TEST_CLEANUP_FIFO_NAME = "zod-test-cleanup"
TEST_RESPONSE_QUERY_FIFO_NAME = "zod-test-response-query"
TEST_RESPONSE_RESULT_FIFO_NAME = "zod-test-response-result"
TEST_CONTROL_READY_NAME = "zod-test-control-ready"


from client_server._harness import (
    ZodFixtureControl,
    _zod_last_tool_text as last_tool_text,
    process_exists,
    process_group_exists,
    release_partial_sideband,
    stop_process,
    stop_process_group,
    stop_process_id,
    wait_for_marker,
)


def test_restart_cancels_partial_sideband_frame(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        descendant_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="start partial sideband descendant", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            marker = wait_for_marker(
                temporary_path,
                "zod-sideband-descendant-pid",
                client,
            )
            descendant_group = int(marker.read_text(encoding="utf-8"))
            release_partial_sideband(marker)
            wait_for_marker(
                temporary_path,
                "zod-sideband-partial-tail-written",
                client,
            )

            restarted = client._start_send(control="restart")
            received = threading.Event()
            errors: list[BaseException] = []

            def receive_restart() -> None:
                try:
                    client._receive(restarted)
                except BaseException as error:
                    errors.append(error)
                finally:
                    received.set()

            receiver = threading.Thread(target=receive_restart, daemon=True)
            receiver.start()
            assert received.wait(FIXTURE_CHECKPOINT_TIMEOUT_SECONDS), (
                "restart waited for a partial sideband frame"
            )
            receiver.join()
            if errors:
                raise errors[0]
            assert last_tool_text(client) == (
                "[active evaluation stopped by session restart request]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            )

            assert not process_group_exists(descendant_group), (
                "partial-sideband descendant outlived sandbox retirement"
            )
            descendant_group = None
            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            stop_process_group(descendant_group)
            if not passed:
                stop_process(client.process)


def test_restart_cancels_reader_after_operation_result(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        descendant_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="complete before partial sideband descendant")
            assert last_tool_text(client) == "[done]"
            marker = wait_for_marker(
                temporary_path,
                "zod-sideband-descendant-pid",
                client,
            )
            descendant_group = int(marker.read_text(encoding="utf-8"))
            wait_for_marker(
                temporary_path,
                "zod-sideband-partial-tail-written",
                client,
            )

            restarted = client._start_send(control="restart")
            received = threading.Event()
            errors: list[BaseException] = []

            def receive_restart() -> None:
                try:
                    client._receive(restarted)
                except BaseException as error:
                    errors.append(error)
                finally:
                    received.set()

            receiver = threading.Thread(target=receive_restart, daemon=True)
            receiver.start()
            assert received.wait(FIXTURE_CHECKPOINT_TIMEOUT_SECONDS), (
                "restart waited for the sideband reader"
            )
            receiver.join()
            if errors:
                raise errors[0]
            assert last_tool_text(client) == (
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            )

            assert not process_group_exists(descendant_group), (
                "partial-sideband descendant outlived sandbox retirement"
            )
            descendant_group = None
            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            stop_process_group(descendant_group)
            if not passed:
                stop_process(client.process)


def test_restart_drains_readable_frame_before_abandoning_partial_tail(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    relay = Path(__file__).resolve().parents[3] / "fixtures" / "delayed_sideband_relay"
    interposer_source = (
        Path(__file__).resolve().parents[3] / "fixtures" / "delay_sideband_poll.c"
    )
    with (
        tempfile.TemporaryDirectory() as temporary_directory,
        ZodFixtureControl(Path(temporary_directory)) as control,
    ):
        temporary = Path(temporary_directory)
        interposer = temporary / "delay-sideband-poll.dylib"
        subprocess.run(
            [
                "cc",
                "-dynamiclib",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                interposer,
                interposer_source,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        loaded_name = "delay-sideband-poll-loaded"
        arm_name = "delay-sideband-poll-arm"
        socket_ready_name = "delay-sideband-poll-socket-ready"
        cancellation_ready_name = "delay-sideband-poll-cancellation-ready"
        partial_tail_name = "zod-sideband-partial-tail-written"
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_RELAY_BINARY"] = str(binary)
        environment["MCP_CONSOLE_TEST_POLL_DYLIB"] = str(interposer)
        environment["MCP_CONSOLE_TEST_POLL_LOADED_NAME"] = loaded_name
        environment["MCP_CONSOLE_TEST_POLL_ARM_NAME"] = arm_name
        environment["MCP_CONSOLE_TEST_POLL_SOCKET_READY_NAME"] = socket_ready_name
        environment["MCP_CONSOLE_TEST_POLL_CANCEL_READY_NAME"] = cancellation_ready_name
        control.configure(environment)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod), "--relay", str(relay)),
            environment,
        )

        descendant_group = None
        cancellation_ready: FifoCheckpoint | None = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="complete silently")
            assert last_tool_text(client) == "[done]"
            control.connect(client)
            loaded = wait_for_marker(temporary, loaded_name, client)
            cancellation_ready = FifoCheckpoint(loaded.parent / cancellation_ready_name)
            (loaded.parent / arm_name).touch()

            evaluation = client._start_send(
                r="wait after readable frame and partial tail"
            )
            marker = wait_for_marker(
                temporary,
                "zod-sideband-descendant-pid",
                client,
            )
            descendant_group = int(marker.read_text(encoding="utf-8"))
            wait_for_marker(temporary, socket_ready_name, client)
            wait_for_marker(temporary, partial_tail_name, client)
            restart = client._start_send(control="restart")
            cancellation_ready.wait(
                "relay sideband cancellation",
                timeout=FIXTURE_CHECKPOINT_TIMEOUT_SECONDS,
            )
            client._receive_many([evaluation, restart])
            result = evaluation["result"]
            assert result["isError"] is True, result
            assert result["content"] == [
                {
                    "type": "text",
                    "text": (
                        "zod readable retirement frame\n"
                        "[stopped by session restart request before evaluation finished]\n"
                        "[worker stopped: in-memory state lost]"
                    ),
                }
            ], result
            restart_result = restart["result"]
            assert restart_result.get("isError") is not True, restart_result
            assert restart_result["content"] == [
                {
                    "type": "text",
                    "text": (
                        "[active evaluation stopped by session restart request]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                }
            ], restart_result

            assert not process_group_exists(descendant_group), (
                "partial-sideband descendant outlived sandbox retirement"
            )
            descendant_group = None
            client.send(r="echo replacement ready")
            assert last_tool_text(client) == "zod: replacement ready\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if cancellation_ready is not None:
                cancellation_ready.close()
            control.release_cleanup()
            stop_process_group(descendant_group)
            if not passed:
                stop_process(client.process)


def test_shutdown_cancels_partial_sideband_frame(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        descendant_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="start partial sideband descendant", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            marker = wait_for_marker(
                temporary_path,
                "zod-sideband-descendant-pid",
                client,
            )
            descendant_group = int(marker.read_text(encoding="utf-8"))
            release_partial_sideband(marker)
            wait_for_marker(
                temporary_path,
                "zod-sideband-partial-tail-written",
                client,
            )

            shutdown_started = time.monotonic()
            client.stdin.close()
            try:
                return_code = client.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    "mcp-console waited for a partial sideband frame"
                ) from None
            shutdown_elapsed = time.monotonic() - shutdown_started

            assert shutdown_elapsed < 1.5, (
                f"worker shutdown took {shutdown_elapsed:.3f} seconds"
            )
            assert return_code == 0, client.stderr.read()
            client.stdout.read()
            assert client.stderr.read() == ""
            assert not process_group_exists(descendant_group), (
                "partial-sideband descendant outlived server shutdown"
            )
            descendant_group = None
            passed = True
            return client.transcript
        finally:
            stop_process_group(descendant_group)
            if not passed:
                stop_process(client.process)


def test_shutdown_deadline_does_not_wait_for_sideband_writer(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with (
        tempfile.TemporaryDirectory() as temporary_directory,
        ZodFixtureControl(Path(temporary_directory)) as control,
    ):
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_BLOCK_NEXT_SIDEBAND_WRITE"] = "1"
        environment["ZOD_RETAIN_BLOCKED_SIDEBAND"] = "1"
        control.configure(environment)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        sideband_holder = None
        passed = False
        try:
            client._initialize_and_list_tools()
            target_operation = client._next_request_id
            # This starts the lazy worker; Zod waits for the control below
            # before reading any byte of the evaluation from its sideband.
            entry = client._start_send(r="x" * (2 * 1024 * 1024))
            assert entry["id"] == target_operation, entry
            control.connect(client)
            control.send_control(
                0,
                "block_next_sideband_write",
                target_operation=target_operation,
            )
            event = control.wait_for(target_operation, "sideband_reader_stalled")
            worker_group = event["process_group"]
            assert isinstance(worker_group, int) and worker_group > 0, event
            holder_marker = wait_for_marker(
                Path(temporary_directory),
                "zod-blocked-sideband-holder-pid",
                client,
            )
            sideband_holder = int(holder_marker.read_text(encoding="utf-8"))
            assert os.getpgid(sideband_holder) == sideband_holder, (
                "sideband holder did not detach from the worker process group"
            )
            entry["send"]["r"] = "<large cell>"
            shutdown_started = time.monotonic()
            client.stdin.close()
            try:
                return_code = client.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    "mcp-console did not enforce its worker shutdown deadline; "
                    + control.diagnostics()
                ) from None
            shutdown_elapsed = time.monotonic() - shutdown_started

            assert shutdown_elapsed < 1.5, (
                f"worker shutdown took {shutdown_elapsed:.3f} seconds"
            )
            assert return_code == 0, client.stderr.read()
            client.stdout.read()
            assert client.stderr.read() == ""
            assert not process_group_exists(worker_group), "Zod outlived mcp-console"
            assert not process_exists(sideband_holder), (
                "sideband holder outlived sandbox lifetime cleanup"
            )
            sideband_holder = None
            passed = True
            return client.transcript
        finally:
            stop_process_id(sideband_holder)
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


if __name__ == "__main__":
    run_this_suite(__file__)
