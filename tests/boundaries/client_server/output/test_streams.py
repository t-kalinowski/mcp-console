#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import (
    large_output,
    last_tool_text,
    remove_length_marker,
)
from support.previews import (
    TEXT_BUDGET,
    assert_preview,
    cell_text,
    compact_previews,
    normalize_pipe_counts,
    normalize_preview_paths,
    session_directory,
)
from support.client import McpClient, stop_client
from support.events import Events
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.checkpoints import FifoCheckpoint, release_fixture_checkpoint
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, requires
from support.suites import run_this_suite

TEST_GATED_RESPONSE_SIZE = 128 * 1024
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)

from boundaries.client_server._harness import (
    ResponseGateObserver,
    SocketGateMcpClient,
    ZodFixtureControl,
    wait_for_marker,
)


@executions(DIRECT, SANDBOXED)
def test_keeps_partial_utf8_across_polls_and_orders_stream_switches(
    binary: Path, execution: Execution
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        roots = ("--writable-root", temporary) if execution == SANDBOXED else ()
        with (
            closing(FifoCheckpoint.create(directory / "partial-release")) as release,
            closing(
                FifoCheckpoint.create(directory / "partial-processed")
            ) as processed,
            McpClient(
                binary,
                execution.serve(
                    "--worker",
                    str(fixtures / "zod"),
                    "--relay",
                    str(fixtures / "server_relay/scripted_relay.py"),
                    *roots,
                ),
                {
                    **os.environ,
                    "TMPDIR": temporary,
                    "MCP_CONSOLE_TEST_PREVIEW_DIRECTORY": temporary,
                    "MCP_CONSOLE_TEST_RELAY_SCENARIO": "partial_utf8_polls",
                },
                current_directory=directory,
            ) as client,
        ):
            try:
                client.initialize_and_list_tools()
                running = "\n[running; poll with an empty send]"
                assert client.send(r="42", timeout_ms=0)["content"] == [
                    {"type": "text", "text": running}
                ]
                session = next((directory / ".agents/console/sessions").iterdir())
                raw = b""
                for data, expected in (
                    (b"A\xe2", "A"),
                    (b"\x82\xacB\xe2", "€B"),
                    (b"C\xf0\x9f", "�C"),
                    (b" D\xe2", "� D"),
                ):
                    release.release()
                    processed.wait("direct bytes reached the output tape")
                    raw += data
                    result = client.send(timeout_ms=0)
                    assert result == {
                        "content": [{"type": "text", "text": expected + running}],
                        "isError": False,
                    }, result
                    assert client.send(timeout_ms=0)["content"] == [
                        {"type": "text", "text": running}
                    ]
                    assert (session / "outputs/call-000001.log").read_bytes() == raw
                release.release()
                assert client.send()["content"] == [{"type": "text", "text": "�"}]
                assert client.send(r="42")["content"] == [
                    {"type": "text", "text": "��"}
                ]
                assert (session / "outputs/call-000011.log").read_bytes() == b"\x82\xac"
                assert client.send()["content"] == [
                    {"type": "text", "text": "\n[idle]"}
                ]
                return client.finish()
            finally:
                for _ in range(5):
                    release.release()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_captures_worker_stdout(binary: Path, execution: Execution) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with (
        tempfile.TemporaryDirectory() as temporary,
        McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            {**os.environ, "TMPDIR": temporary},
        ) as client,
    ):
        client.initialize_and_list_tools()
        request = client.start_send(r="emit stdout")
        release = wait_for_marker(
            Path(temporary), "zod-release-stdout-completion", client
        )
        expected = large_output("zod stdout 👩🏽‍💻\n")
        recorded = session_directory(client) / "outputs/call-000001.log"
        # stdout and completion use independent transports. A completed write
        # does not prove the server has captured the pipe's remaining bytes.
        deadline = time.monotonic() + 10
        with Events() as events:
            events.watch_file(recorded)
            events.watch_process(client.process.pid)
            while recorded.stat().st_size < len(expected.encode()):
                assert client.process.poll() is None, "server exited before capture"
                remaining = deadline - time.monotonic()
                assert remaining > 0 and events.wait(remaining), (
                    "server did not capture the complete stdout payload"
                )
        assert recorded.read_bytes() == expected.encode()
        release_fixture_checkpoint(release)
        client.receive(request)
        assert_preview(last_tool_text(client), expected)
        normalize_preview_paths(client)
        compact_previews(client, "x", "y", "z", "s", "p", "ab")
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_compacts_each_polled_output_segment(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()

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
        compact_previews(client, "x", "y", "z", "s", "p", "ab")
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_compacts_many_redraws_in_one_response(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
    )
    client.initialize_and_list_tools()

    client.send(r="stress redraws")
    assert last_tool_text(client) == "stress final\nuseful output\n"
    compact_previews(client, "x", "y", "z", "s", "p", "ab")
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_preserves_invalid_raw_output_when_worker_exits(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
    )
    client.initialize_and_list_tools()

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
        raw, recorded_tail = remove_length_marker(
            cell_text(client, 1 if stream == "stdout" else 2), marker_prefix
        )
        assert recorded_tail == tail_size
        assert raw == large_output(prefix) + ("z" * tail_size), (
            f"worker crash lost {stream} bytes"
        )
        assert_preview(raw_output, raw)
        normalize_pipe_counts(client)

    compact_previews(client, "x", "y", "z", "s", "p", "ab")
    return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_preserves_raw_output_during_malformed_sideband_failure(
    binary: Path,
    execution: Execution,
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as temporary_directory:
        interposer = build_interposer(
            Path(temporary_directory), "relay_stdout_read_interposer"
        )
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_RELAY_BINARY"] = str(binary)
        environment["MCP_CONSOLE_TEST_RELAY_READ_DYLIB"] = str(interposer)
        environment["MCP_CONSOLE_TEST_RELAY_READ_MATCH"] = "zod malformed output read\n"
        with McpClient(
            binary,
            execution.serve(
                "--worker",
                str(fixtures / "zod"),
                "--relay",
                str(fixtures / "retirement_read_relay"),
            ),
            environment,
        ) as client:
            client.initialize_and_list_tools()

            for stream in ("stdout", "stderr"):
                client.send(r=f"malformed sideband after {stream}")
                result = client.transcript[-1]["result"]
                assert result["isError"] is True, result
                output = result["content"][0]["text"]
                marker_prefix = f"zod expected {stream} malformed tail: "
                output, tail_size = remove_length_marker(output, marker_prefix)
                prefix = f"zod malformed {stream}: "
                raw = (
                    large_output(prefix)
                    + ("z" * tail_size)
                    + "zod malformed output read\n"
                )
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
                recorded, recorded_tail = remove_length_marker(
                    cell_text(client, 1 if stream == "stdout" else 2), marker_prefix
                )
                assert recorded_tail == tail_size
                assert recorded == raw, f"malformed frame lost {stream} bytes"
                assert all(output.count(notice) == 1 for notice in notices), repr(
                    output
                )
                assert [output.index(notice) for notice in notices] == sorted(
                    output.index(notice) for notice in notices
                )
                assert output.endswith("\n".join(notices)), output[-500:]
                assert_preview(output.removesuffix("\n".join(notices)), raw)
                normalize_pipe_counts(client)

            compact_previews(client, "x", "y", "z", "s", "p", "ab")
            transcript, standard_error = client.finish_with_standard_error()
            diagnostics = standard_error.splitlines()
            # Relay stderr is diagnostic-only and can be cut off when the server's
            # fail-safe stops a failed generation. The framed failure above is authoritative.
            assert len(diagnostics) <= 2, standard_error
            assert all(
                diagnostic.startswith("worker sideband read failed: ")
                for diagnostic in diagnostics
            ), standard_error
            return transcript


@executions(DIRECT, SANDBOXED)
def test_preserves_raw_output_during_semantically_invalid_sideband_message(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
    )
    client.initialize_and_list_tools()

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
    recorded, recorded_tail = remove_length_marker(cell_text(client, 1), marker_prefix)
    assert recorded_tail == tail_size
    assert recorded == raw, "semantic failure lost raw stdout bytes"
    assert all(output.count(notice) == 1 for notice in notices), repr(output)
    assert [output.index(notice) for notice in notices] == sorted(
        output.index(notice) for notice in notices
    )
    assert output.endswith("\n".join(notices)), output[-500:]
    # The builder terminates the raw line before its failure notices.
    assert_preview(output.removesuffix("\n".join(notices)).removesuffix("\n"), raw)
    normalize_pipe_counts(client)
    compact_previews(client, "x", "y", "z", "s", "p", "ab")
    return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_drains_background_stderr_while_idle(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
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
        assert len(output.encode()) <= TEXT_BUDGET
        assert "no retained cell log" in output
        assert "outputs/call-" not in output
        assert cell_text(client, 1) == ""
        assert_preview(
            output.removesuffix("\n[idle]"), large_output("zod background stderr\n")
        )
        compact_previews(client, "x", "y", "z", "s", "p", "ab")
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_times_out_and_polls_running_evaluation(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
    )
    client.initialize_and_list_tools()
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
    compact_previews(client, "x", "y", "z", "s", "p", "ab")
    return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_drains_pending_sideband_output_while_running(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()

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
        assert client.temporary_directory is not None
        workspace = Path(client.temporary_directory.name)
        session = next((workspace / ".agents/console" / "sessions").iterdir())
        assert (session / "outputs" / "call-000001.log").read_text(
            encoding="utf-8"
        ) == "before pending image\nafter pending image\n"

        (image_started.parent / "zod-release-image-completion").touch()
        client.send(timeout_ms=3_000)
        assert last_tool_text(client) == "[done]"
        compact_previews(client, "x", "y", "z", "s", "p", "ab")
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_orders_queued_cancellation_behind_incomplete_response(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment = os.environ.copy()
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        write_reached = FifoCheckpoint.create(temporary / "response-write-reached")
        write_release = FifoCheckpoint.create(temporary / "response-write-release")
        environment[LOADER_VARIABLE] = str(
            build_interposer(temporary, "response_write_interposer")
        )
        environment["MCP_CONSOLE_TEST_RESPONSE_WRITE_REACHED"] = str(write_reached.path)
        environment["MCP_CONSOLE_TEST_RESPONSE_WRITE_RELEASE"] = str(write_release.path)
        release = temporary / "response-gate-released"
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_TEST_RESPONSE_GATE_RELEASED"] = str(release)
        with ZodFixtureControl(temporary) as control:
            control.configure(environment)
            client = SocketGateMcpClient(
                binary,
                execution.serve("--worker", str(zod)),
                environment,
                temporary,
            )
            observer: ResponseGateObserver | None = None
            finished = False
            try:
                client.initialize_and_list_tools()
                observer = ResponseGateObserver(
                    temporary,
                    client.stdout.stream,
                    release,
                )

                invalid_requirement = (
                    "https://invalid.example/" + "x" * TEST_GATED_RESPONSE_SIZE
                )
                first_id = client._next_request_id
                first = client.start_send(
                    requirements={"python": [invalid_requirement]}
                )
                assert first["id"] == first_id, first
                write_reached.wait("response prefix written")
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
                cancelled = client.start_send(r=f"check response gate: {cancelled_id}")
                assert cancelled["id"] == cancelled_id, cancelled
                client.wait_until_input_is_read(
                    f"cancelled request {cancelled_id}", control
                )

                client.notify(
                    "notifications/cancelled",
                    requestId=cancelled_id,
                    reason="cancel before worker admission",
                )
                client.wait_until_input_is_read(
                    f"cancellation for request {cancelled_id}", control
                )
                control.record_client_event(cancelled_id, "operation_accepted")

                live_id = client._next_request_id
                live = client.start_send(r=f"check response gate: {live_id}")
                assert live["id"] == live_id, live
                client.wait_until_input_is_read(f"live request {live_id}", control)
                control.record_client_event(
                    cancelled_id,
                    "cancellation_observed_before_worker_admission",
                )

                barrier_target = 1_000_000
                client.notify(
                    "notifications/cancelled",
                    requestId=barrier_target,
                    reason="staged receive barrier",
                )
                client.wait_until_input_is_read("staged receive barrier", control)
                control.record_client_event(live_id, "operation_accepted")

                write_release.release()
                client.stdout.release_completed_response(
                    first_id,
                    release,
                    control.diagnostics(),
                )
                observer.finish()
                control.record_client_event(first_id, "response_write_completed")
                client.receive(first)
                assert first["result"]["isError"] is True
                error = first["result"]["content"][0]["text"]
                assert len(error.encode()) <= TEXT_BUDGET
                assert error.startswith("Python requirement `https://invalid.example/")
                assert error.endswith(
                    "host-side managed resolution accepts named package requirements only"
                )
                first["send"]["requirements"]["python"] = [
                    "<large invalid Python requirement>"
                ]

                control.connect(client)
                started = control.wait_for(live_id, "worker_operation_started")
                assert started["response_gate_released"] is True, control.diagnostics()
                control.wait_for(live_id, "worker_operation_completed")
                client.receive(live)
                assert live["result"] == {
                    "content": [
                        {
                            "type": "text",
                            "text": "zod response-gated operation\n",
                        }
                    ],
                    "isError": False,
                }, live

                ping = client.request("ping")
                assert ping["result"] == {}, ping
                client.close_input_observer()
                transcript = client.finish()
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
                write_release.release()
                write_reached.close()
                write_release.close()
                if not finished:
                    stop_client(client)
                if observer is not None:
                    observer.close()
                client.close_test_stdio()


if __name__ == "__main__":
    run_this_suite(__file__)
