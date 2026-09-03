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
    ResponseGateObserver,
    SocketGateMcpClient,
    ZodFixtureControl,
    assert_large_output,
    large_output,
    _zod_last_tool_text as last_tool_text,
    remove_length_marker,
    wait_for_marker,
)


def test_captures_worker_stdout(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(r="emit stdout")
    output = last_tool_text(client)
    assert_large_output(output, "zod stdout 👩🏽‍💻\n")
    client.transcript[-1]["result"]["content"][0]["text"] = (
        "zod stdout 👩🏽‍💻\n<large output>\n"
    )
    return client._finish()


def test_compacts_split_terminal_redraws(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="emit terminal redraws")
    assert last_tool_text(client) == "ordinary stdout\r\nol\nnew\nold\x1b[2Knew\n"
    return client._finish()


def test_compacts_stdout_and_stderr_independently(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="emit independent stdout stderr redraws")
    output = last_tool_text(client)
    lines = sorted(output.splitlines(keepends=True))
    assert lines == ["stderr final\n", "stdout final\n"], output
    client.transcript[-1]["result"]["content"][0]["text"] = "".join(lines)
    client.transcript[-1]["transcript_normalization"] = {
        "target": "result.content[0].text",
        "cross_source_position": "omitted",
    }
    return client._finish()


def test_compacts_each_polled_output_segment(
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
        client._initialize_and_list_tools()

        client.send(r="redraw across polls", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        marker = wait_for_marker(
            temporary_path,
            "zod-redraw-ready",
            client,
        )

        client.send(timeout_ms=0)
        assert last_tool_text(client) == (
            "output 10%\n[running; poll with an empty send]"
        )
        client.send(timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"

        (marker.parent / "zod-release-redraw").touch()
        client.send(timeout_ms=3_000)
        assert last_tool_text(client) == "output 100%\n"
        return client._finish()


def test_compacts_many_redraws_in_one_response(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="stress redraws")
    assert last_tool_text(client) == "stress final\nuseful output\n"
    return client._finish()


def test_preserves_invalid_raw_output_when_worker_exits(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    for stream in ("stdout", "stderr"):
        client.send(r=f"exit after invalid {stream}")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        failure = (
            "\n[worker sideband read failed: worker sideband closed]\n"
            "[worker exited with status 86]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )
        output = result["content"][0]["text"]
        assert output.endswith(failure), output[-200:]
        prefix = f"zod invalid {stream}: � trailing: �"
        raw_output = output.removesuffix(failure)
        marker_prefix = f"zod expected {stream} crash tail: "
        raw_output, tail_size = remove_length_marker(raw_output, marker_prefix)
        assert raw_output == large_output(prefix) + ("z" * tail_size), (
            f"worker crash lost {stream} bytes"
        )
        result["content"][0]["text"] = prefix + "<large output>" + failure

    return client._finish()


def test_preserves_raw_output_during_malformed_sideband_failure(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    for stream in ("stdout", "stderr"):
        client.send(r=f"malformed sideband after {stream}")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        output = result["content"][0]["text"]
        marker_prefix = f"zod expected {stream} malformed tail: "
        output, tail_size = remove_length_marker(output, marker_prefix)
        prefix = f"zod malformed {stream}: "
        raw = large_output(prefix) + ("z" * tail_size)
        failure_start = output.find("[worker sideband read failed: ")
        assert failure_start >= 0, output[-200:]
        failure_end = output.find("\n", failure_start)
        assert failure_end >= 0, output[-200:]
        failure = output[failure_start:failure_end]
        notices = [
            failure,
            "[worker terminated by signal 9]",
            "[worker stopped: in-memory state lost]",
            "[starting new worker]",
            "[idle]",
        ]
        assert output.count(raw) == 1, f"malformed frame lost {stream} bytes"
        assert all(output.count(notice) == 1 for notice in notices), repr(output)
        assert [output.index(notice) for notice in notices] == sorted(
            output.index(notice) for notice in notices
        ), repr(output)
        remainder = output.replace(raw, "")
        for notice in notices:
            remainder = remainder.replace(notice, "")
        assert not remainder.replace("\n", ""), repr(output)
        result["content"][0]["text"] = (
            f"{prefix}<large output>\n"
            "[worker sideband read failed: <invalid frame>]\n"
            "[worker terminated by signal 9]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n[idle]"
        )
        client.transcript[-1]["transcript_normalization"] = {
            "target": "result.content[0].text",
            "cross_source_position": "omitted",
            "replacements": {
                "large_output": "<large output>",
                "sideband_failure_detail": "<invalid frame>",
            },
        }

    transcript, standard_error = client._finish_with_standard_error()
    diagnostics = standard_error.splitlines()
    # Relay stderr is diagnostic-only and can be cut off when the server's
    # fail-safe stops a failed generation. The framed failure above is authoritative.
    assert len(diagnostics) <= 2, standard_error
    assert all(
        diagnostic.startswith("worker sideband read failed: ")
        for diagnostic in diagnostics
    ), standard_error
    return transcript


def test_preserves_raw_output_during_semantically_invalid_sideband_message(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="unexpected input receipt after stdout")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    output = result["content"][0]["text"]
    marker_prefix = "zod expected semantic tail: "
    output, tail_size = remove_length_marker(output, marker_prefix)
    prefix = "zod unexpected input receipt: "
    raw = large_output(prefix) + ("z" * tail_size)
    notices = [
        "[worker reported received input without requesting it]",
        "[worker terminated by signal 9]",
        "[worker stopped: in-memory state lost]",
        "[starting new worker]",
        "[idle]",
    ]
    assert output.count(raw) == 1, "semantic failure lost raw stdout bytes"
    assert all(output.count(notice) == 1 for notice in notices), repr(output)
    assert [output.index(notice) for notice in notices] == sorted(
        output.index(notice) for notice in notices
    ), repr(output)
    remainder = output.replace(raw, "")
    for notice in notices:
        remainder = remainder.replace(notice, "")
    assert not remainder.replace("\n", ""), repr(output)
    result["content"][0]["text"] = f"{prefix}<large output>\n" + "\n".join(notices)
    client.transcript[-1]["transcript_normalization"] = {
        "target": "result.content[0].text",
        "cross_source_position": "omitted",
        "replacements": {"large_output": "<large output>"},
    }
    return client._finish()


def test_drains_background_stderr_while_idle(binary: Path) -> Transcript:
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
        client._initialize_and_list_tools()
        client.send(r="start background stderr")
        assert last_tool_text(client) == "[done]"
        started = wait_for_marker(
            temporary_path,
            "zod-background-stderr-started",
            client,
        )
        (started.parent / "zod-release-background-stderr").touch()
        wait_for_marker(
            temporary_path,
            "zod-background-stderr-emitted",
            client,
        )

        client.send(timeout_ms=0)
        output = last_tool_text(client)
        assert output.endswith("\n[idle]"), output[-100:]
        assert_large_output(
            output.removesuffix("\n[idle]"),
            "zod background stderr\n",
        )
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "zod background stderr\n<large output>\n[idle]"
        )
        return client._finish()


def test_times_out_and_polls_running_evaluation(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(r="echo echo")
    client.send(
        r="complete after timeout",
        timeout_ms=10,
    )
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "\n[running; poll with an empty send]", output
    client.send(timeout_ms=3_000)
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "zod: complete after timeout\n", output
    client.send(r="echo echo")
    return client._finish()


def test_drains_pending_sideband_output_while_running(binary: Path) -> Transcript:
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
        client._initialize_and_list_tools()

        client.send(r="emit output and image before completion", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        image_started = wait_for_marker(
            temporary_path,
            "zod-image-evaluation-started",
            client,
        )
        (image_started.parent / "zod-release-image").touch()
        wait_for_marker(temporary_path, "zod-image-processed", client)

        client.send(timeout_ms=0)
        result = client.transcript[-1]["result"]
        assert result == {
            "content": [
                {"type": "text", "text": "before pending image\n"},
                {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                {
                    "type": "text",
                    "text": "after pending image\n\n[running; poll with an empty send]",
                },
            ],
            "isError": False,
        }, result

        (image_started.parent / "zod-release-image-completion").touch()
        client.send(timeout_ms=3_000)
        assert last_tool_text(client) == "[done]"
        return client._finish()


def test_orders_queued_cancellation_behind_incomplete_response(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment = os.environ.copy()
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        release = temporary / "response-gate-released"
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_TEST_RESPONSE_GATE_RELEASED"] = str(release)
        with ZodFixtureControl(temporary) as control:
            control.configure(environment)
            client = SocketGateMcpClient(
                binary,
                ("serve", "--worker", str(zod)),
                environment,
                temporary,
            )
            observer: ResponseGateObserver | None = None
            finished = False
            try:
                client._initialize_and_list_tools()
                observer = ResponseGateObserver(
                    temporary,
                    client.stdout.stream,
                    release,
                )

                invalid_requirement = (
                    "https://invalid.example/" + "x" * TEST_GATED_RESPONSE_SIZE
                )
                first_id = client._next_request_id
                first = client._start_send(
                    requirements={"python": [invalid_requirement]}
                )
                assert first["id"] == first_id, first
                buffered = client.stdout.wait_for_incomplete_response(
                    first_id,
                    len(invalid_requirement),
                    control.diagnostics(),
                )
                control.record_client_event(
                    first_id,
                    "response_writer_reached_gate",
                    buffered_bytes=buffered,
                )

                cancelled_id = client._next_request_id
                cancelled = client._start_send(r=f"check response gate: {cancelled_id}")
                assert cancelled["id"] == cancelled_id, cancelled
                client.wait_until_input_is_read(
                    f"cancelled request {cancelled_id}", control
                )

                client._notify(
                    "notifications/cancelled",
                    requestId=cancelled_id,
                    reason="cancel before worker admission",
                )
                client.wait_until_input_is_read(
                    f"cancellation for request {cancelled_id}", control
                )
                control.record_client_event(cancelled_id, "operation_accepted")

                live_id = client._next_request_id
                live = client._start_send(r=f"check response gate: {live_id}")
                assert live["id"] == live_id, live
                client.wait_until_input_is_read(f"live request {live_id}", control)
                control.record_client_event(
                    cancelled_id,
                    "cancellation_observed_before_worker_admission",
                )

                barrier_target = 1_000_000
                client._notify(
                    "notifications/cancelled",
                    requestId=barrier_target,
                    reason="staged receive barrier",
                )
                client.wait_until_input_is_read("staged receive barrier", control)
                control.record_client_event(live_id, "operation_accepted")

                client.stdout.release_completed_response(
                    first_id,
                    release,
                    control.diagnostics(),
                )
                observer.finish()
                control.record_client_event(first_id, "response_write_completed")
                client._receive(first)
                expected_error = (
                    f"Python requirement `{invalid_requirement}` is not accepted: "
                    "host-side managed resolution accepts named package "
                    "requirements only"
                )
                assert first["result"] == {
                    "content": [{"type": "text", "text": expected_error}],
                    "isError": True,
                }, first
                first["send"]["requirements"]["python"] = [
                    "<large invalid Python requirement>"
                ]
                first["result"]["content"][0]["text"] = (
                    "<large invalid Python requirement rejected>"
                )

                control.connect(client)
                started = control.wait_for(live_id, "worker_operation_started")
                assert started["response_gate_released"] is True, control.diagnostics()
                control.wait_for(live_id, "worker_operation_completed")
                client._receive(live)
                assert live["result"] == {
                    "content": [
                        {
                            "type": "text",
                            "text": "zod response-gated operation\n",
                        }
                    ],
                    "isError": False,
                }, live

                ping = client._request("ping")
                assert ping["result"] == {}, ping
                client.close_input_observer()
                transcript = client._finish()
                control.wait_for_eof()
                cancelled_events = [
                    event
                    for event in control.events
                    if event.get("operation") == cancelled_id
                    and event.get("component") == "fixture"
                ]
                assert not cancelled_events, control.diagnostics()
                control.assert_before(
                    (cancelled_id, "cancellation_observed_before_worker_admission"),
                    (live_id, "worker_operation_started"),
                )
                finished = True
                return transcript
            finally:
                if not finished:
                    stop_client(client)
                if observer is not None:
                    observer.close()
                client.close_test_stdio()


if __name__ == "__main__":
    run_this_suite(__file__)
