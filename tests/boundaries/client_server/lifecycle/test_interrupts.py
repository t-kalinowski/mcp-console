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
    continue_stopped_worker,
    _zod_last_tool_text as last_tool_text,
    resolver_interrupt_permission_environment,
    stop_process,
    stop_process_group,
    stop_recorded_worker,
    wait_for_path,
    wait_for_process_group_exit,
    wait_for_stopped_worker,
    wait_for_worker_retirement,
)


def test_interrupts_running_worker_with_sigint(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment = os.environ.copy()
    with ZodFixtureControl() as control:
        control.configure(environment)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        finished = False
        try:
            client._initialize_and_list_tools()

            target_id = client._next_request_id
            client.send(
                r=f"wait for interrupt: {target_id}",
                timeout_ms=0,
            )
            assert client.transcript[-1]["id"] == target_id
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            control.connect(client)
            control.wait_for(target_id, "worker_operation_started")

            interrupt_id = client._next_request_id
            client.send(control="interrupt", timeout_ms=3_000)
            assert client.transcript[-1]["id"] == interrupt_id
            assert last_tool_text(client) == "zod interrupted\n"
            observed = control.wait_for(target_id, "worker_interrupt_observed")
            assert observed["signal"] == signal.SIGINT, observed
            control.wait_for(target_id, "worker_operation_completed")
            control.record_client_event(
                interrupt_id,
                "interrupt_response_received",
                target_operation=target_id,
            )
            control.assert_before(
                (target_id, "worker_interrupt_observed"),
                (interrupt_id, "interrupt_response_received"),
            )

            checkpoint_id = client._next_request_id
            client.send(r=f"checkpoint {checkpoint_id}")
            assert client.transcript[-1]["id"] == checkpoint_id
            assert last_tool_text(client) == "[done]"
            control.wait_for(checkpoint_id, "worker_operation_completed")
            control.assert_before(
                (target_id, "worker_operation_completed"),
                (checkpoint_id, "worker_operation_started"),
            )

            transcript = client._finish()
            finished = True
            return transcript
        finally:
            if not finished:
                stop_client(client)


def test_supervises_stopped_and_continued_workers(binary: Path) -> Transcript:
    wrapper = Path(__file__).resolve().parents[3] / "fixtures" / "stop_continue_zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_control.write_text("stop evaluation", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        client = McpClient(
            binary,
            ("serve", "--worker", str(wrapper)),
            environment,
        )
        workers: list[tuple[int, int]] = []
        passed = False
        try:
            client._initialize_and_list_tools()
            evaluation = client._start_send(r="echo echo")
            marker, worker_pid, worker_group = wait_for_stopped_worker(
                temporary_path,
                set(),
                workers,
                client,
            )

            interrupt = client._start_send(control="interrupt", timeout_ms=0)
            readable, _, _ = select.select(
                [client.stdout],
                [],
                [],
                FIXTURE_CHECKPOINT_TIMEOUT_SECONDS,
            )
            assert readable, "relay supervision did not answer the interrupt request"
            client._receive(interrupt)
            assert interrupt["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": "worker evaluation is already being polled",
                    }
                ],
                "isError": True,
            }, interrupt

            continue_stopped_worker(worker_pid, worker_group)
            wait_for_path(
                marker.with_name("zod-stop-continue-resumed"),
                "stopped worker to resume",
                client,
            )
            client._receive(evaluation)
            assert evaluation["result"] == {
                "content": [{"type": "text", "text": "zod: echo\n"}],
                "isError": False,
            }, evaluation

            startup_control.write_text("stop startup", encoding="utf-8")
            restarted = client._start_send(control="restart")
            replacement_marker, replacement_pid, replacement_group = (
                wait_for_stopped_worker(
                    temporary_path,
                    {worker_pid},
                    workers,
                    client,
                )
            )
            assert replacement_group != worker_group, (
                "replacement reused the retiring process group"
            )
            wait_for_worker_retirement(worker_pid, worker_group, client)

            continue_stopped_worker(replacement_pid, replacement_group)
            wait_for_path(
                replacement_marker.with_name("zod-stop-continue-resumed"),
                "replacement worker to resume",
                client,
            )
            client._receive(restarted)
            assert restarted["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[worker stopped: in-memory state lost]\n"
                            "[starting new worker]\n"
                            "[idle]"
                        ),
                    }
                ],
                "isError": False,
            }, restarted

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                for recorded_pid, recorded_group in reversed(workers):
                    stop_recorded_worker(recorded_pid, recorded_group)
                stop_process(client.process)


def test_reports_resolver_interrupt_permission_error(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        (
            environment,
            resolver_started,
            resolver_lifetime,
            resolver_group_record,
            denied_interrupt,
        ) = resolver_interrupt_permission_environment(temporary_path)

        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        resolver_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            preparation = client._start_send(
                requirements={"r": ["blocked-resolver"]},
            )
            resolver_started.wait("permission-denied R resolver")
            resolver_group = int(resolver_group_record.read_text(encoding="utf-8"))
            assert resolver_group != os.getpgrp(), (
                "resolver did not enter a dedicated process group"
            )

            interrupt = client._start_send(control="interrupt", timeout_ms=0)
            responses_returned = threading.Event()
            forced_stop = threading.Event()

            def stop_if_calls_block() -> None:
                if not responses_returned.wait(2):
                    forced_stop.set()
                    stop_process_group(resolver_group)

            watchdog = threading.Thread(target=stop_if_calls_block, daemon=True)
            watchdog.start()
            try:
                client._receive_many([preparation, interrupt])
            finally:
                responses_returned.set()
                watchdog.join()

            denied_group = int(denied_interrupt.read_text(encoding="utf-8"))
            assert denied_group == resolver_group, (
                "SIGINT denial targeted a different process group"
            )
            wait_for_process_group_exit(resolver_group, client)
            assert not forced_stop.is_set(), (
                "resolver interrupt failure did not terminate both calls"
            )

            expected = {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "failed to interrupt R package resolver `ir`: "
                            "Operation not permitted (os error 1)"
                        ),
                    }
                ],
                "isError": True,
            }
            assert preparation["result"] == expected, preparation
            interrupt_expected = {
                "content": [
                    {
                        "type": "text",
                        "text": f"[{expected['content'][0]['text']}]",
                    }
                ],
                "isError": True,
            }
            assert interrupt["result"] == interrupt_expected, interrupt

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(resolver_group)
                stop_client(client)
            resolver_started.close()
            resolver_lifetime.close()


def test_reports_runtime_r_resolver_interrupt_permission_error(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        (
            environment,
            resolver_started,
            resolver_lifetime,
            resolver_group_record,
            denied_interrupt,
        ) = resolver_interrupt_permission_environment(temporary_path)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        resolver_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            evaluation = client._start_send(
                r="report runtime R resolution failure",
            )
            resolver_started.wait("permission-denied runtime R resolver")
            resolver_group = int(resolver_group_record.read_text(encoding="utf-8"))
            assert resolver_group != os.getpgrp(), (
                "resolver did not enter a dedicated process group"
            )

            interrupt = client._start_send(control="interrupt", timeout_ms=0)
            responses_returned = threading.Event()
            forced_stop = threading.Event()

            def stop_if_calls_block() -> None:
                if not responses_returned.wait(2):
                    forced_stop.set()
                    stop_process_group(resolver_group)

            watchdog = threading.Thread(target=stop_if_calls_block, daemon=True)
            watchdog.start()
            try:
                client._receive_many([evaluation, interrupt])
            finally:
                responses_returned.set()
                watchdog.join()

            denied_group = int(denied_interrupt.read_text(encoding="utf-8"))
            assert denied_group == resolver_group, (
                "SIGINT denial targeted a different process group"
            )
            wait_for_process_group_exit(resolver_group, client)
            assert not forced_stop.is_set(), (
                "resolver interrupt failure did not terminate both calls"
            )

            message = (
                "failed to interrupt R package resolver `ir`: "
                "Operation not permitted (os error 1)"
            )
            assert evaluation["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": f"zod R resolution failure: host: {message}\n",
                    }
                ],
                "isError": False,
            }, evaluation
            assert interrupt["result"] == {
                "content": [{"type": "text", "text": f"[{message}]"}],
                "isError": True,
            }, interrupt

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(resolver_group)
                stop_client(client)
            resolver_started.close()
            resolver_lifetime.close()


if __name__ == "__main__":
    run_this_suite(__file__)
