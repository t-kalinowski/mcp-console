#!/usr/bin/env -S uv run --script

"""Public MCP proof for response-gated cancellation and wire-order admission.

On macOS/Linux this replaces the Rust executions of
response_gate_does_not_delay_cancellation_notifications and
response_gate_skips_cancelled_calls_without_reordering. Other targets retain
those Rust cases until equivalent response-write checkpoints are available.
"""

import os
import socket
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server._harness import (
    TEST_GATED_RESPONSE_SIZE,
    ResponseGateObserver,
    SocketGateMcpClient,
    ZodFixtureControl,
    queued_socket_bytes,
)
from support.checkpoints import FifoCheckpoint
from support.client import stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, compile_interposer
from support.previews import TEXT_BUDGET
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, requires
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_keeps_wire_order_across_cancelled_waiting_send(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        initial_reached = FifoCheckpoint.create(temporary / "initial-write-reached")
        initial_release = FifoCheckpoint.create(temporary / "initial-write-release")
        live_reached = FifoCheckpoint.create(temporary / "live-write-reached")
        live_release = FifoCheckpoint.create(temporary / "live-write-release")
        initial_completed = temporary / "initial-response-completed"
        live_completed = temporary / "live-response-completed"
        environment = os.environ.copy()
        environment["MCP_CONSOLE_HOME"] = str(temporary / "console-home")
        environment[LOADER_VARIABLE] = str(
            compile_interposer(
                Path(__file__).with_name("_response_admission.c"),
                temporary / "response-admission",
            )
        )
        environment.update(
            MCP_CONSOLE_TEST_INITIAL_WRITE_REACHED=str(initial_reached.path),
            MCP_CONSOLE_TEST_INITIAL_WRITE_RELEASE=str(initial_release.path),
            MCP_CONSOLE_TEST_LIVE_WRITE_REACHED=str(live_reached.path),
            MCP_CONSOLE_TEST_LIVE_WRITE_RELEASE=str(live_release.path),
            ZOD_TEST_RESPONSE_GATE_RELEASED=str(initial_completed),
        )
        with ZodFixtureControl(temporary) as control:
            control.configure(environment)
            client = SocketGateMcpClient(
                binary,
                execution.serve("--worker", str(zod)),
                environment,
                temporary,
            )
            observers: list[ResponseGateObserver] = []
            finished = False
            try:
                client.initialize_and_list_tools()
                observers.append(
                    ResponseGateObserver(
                        temporary, client.stdout.stream, initial_completed
                    )
                )
                invalid_requirement = (
                    "https://invalid.example/" + "x" * TEST_GATED_RESPONSE_SIZE
                )
                initial = client.start_send(
                    requirements={"python": [invalid_requirement]}
                )
                initial_reached.wait("initial response prefix written")
                client.stdout.wait_for_incomplete_response(
                    initial["id"], len(invalid_requirement), control.diagnostics()
                )

                # Stage each frame separately. Reading the next frame proves that
                # the previous one passed through synchronous transport receipt.
                first_id = client._next_request_id
                first = client.start_send(r=f"check response gate: {first_id}")
                client.wait_until_input_is_read("first live send", control)
                cancelled_id = client._next_request_id
                cancelled = client.start_send(r=f"checkpoint {cancelled_id}")
                client.wait_until_input_is_read("cancelled send", control)
                second_id = client._next_request_id
                second = client.start_send(r=f"check response gate: {second_id}")
                client.wait_until_input_is_read("second live send", control)
                client.notify(
                    "notifications/cancelled",
                    requestId=cancelled_id,
                    reason="cancel before worker admission",
                )
                client.wait_until_input_is_read("cancellation", control)
                client.notify(
                    "notifications/cancelled",
                    requestId=1_000_000,
                    reason="staged receive barrier",
                )
                client.wait_until_input_is_read("staged receive barrier", control)

                initial_release.release()
                client.stdout.release_completed_response(
                    initial["id"], initial_completed, control.diagnostics()
                )
                observers[0].finish()
                client.receive(initial)
                assert initial["result"]["isError"] is True, initial
                error = initial["result"]["content"][0]["text"]
                assert len(error.encode()) <= TEXT_BUDGET
                assert error.startswith("Python requirement `https://invalid.example/")
                assert error.endswith(
                    "host-side managed resolution accepts named package requirements only"
                )
                initial["send"]["requirements"]["python"] = [
                    "<large invalid Python requirement>"
                ]

                control.connect(client)
                live_reached.wait("first live response prefix written")
                # Reading the initial response may also buffer the live prefix.
                pending = queued_socket_bytes(client.stdout.stream)
                prefix = bytes(client.stdout.buffer) + (
                    client.stdout.stream.recv(pending, socket.MSG_PEEK)
                    if pending
                    else b""
                )
                assert 0 < len(prefix) < TEXT_BUDGET, prefix
                assert f'"id":{first_id},'.encode() in prefix, prefix
                assert b"\n" not in prefix, prefix
                first_started = control.wait_for(first_id, "worker_operation_started")
                assert first_started["response_gate_released"] is True, (
                    control.diagnostics()
                )
                control.wait_for(first_id, "worker_operation_completed")
                # A completed worker operation still owns admission until its
                # response write finishes. The successor queries this actual
                # response, including when a broken cancellation chain lets it
                # start while the write remains blocked.
                observers.append(
                    ResponseGateObserver(
                        temporary, client.stdout.stream, live_completed
                    )
                )
                live_release.release()
                client.stdout.release_completed_response(
                    first_id, live_completed, control.diagnostics()
                )
                observers[1].finish()
                second_started = control.wait_for(second_id, "worker_operation_started")
                assert second_started["response_gate_released"] is True, (
                    control.diagnostics()
                )
                control.wait_for(second_id, "worker_operation_completed")

                expected = {
                    "content": [
                        {"type": "text", "text": "zod response-gated operation\n"}
                    ],
                    "isError": False,
                }
                client.receive(first)
                assert first["result"] == expected, first
                client.receive(second)
                assert second["result"] == expected, second
                ping = client.request("ping")
                assert ping["result"] == {}, ping
                client.close_input_observer()
                transcript = client.finish()
                control.wait_for_eof()
                started = [
                    event["operation"]
                    for event in control.events
                    if event["kind"] == "worker_operation_started"
                ]
                assert started == [first_id, second_id], control.diagnostics()
                assert not [
                    event
                    for event in control.events
                    if event["operation"] == cancelled_id
                    and event["component"] == "fixture"
                ], control.diagnostics()
                assert "result" not in cancelled and "error" not in cancelled, cancelled
                control.assert_before(
                    (first_id, "worker_operation_completed"),
                    (second_id, "worker_operation_started"),
                )
                transcript.append(
                    {
                        "response_admission": {
                            "execution_order": ["first live send", "second live send"],
                            "cancelled_send_executed": False,
                            "predecessor_response_completed_at_start": [
                                first_started["response_gate_released"],
                                second_started["response_gate_released"],
                            ],
                        }
                    }
                )
                finished = True
                return transcript
            finally:
                initial_release.release()
                live_release.release()
                for checkpoint in (
                    initial_reached,
                    initial_release,
                    live_reached,
                    live_release,
                ):
                    checkpoint.close()
                if not finished:
                    stop_client(client)
                for observer in observers:
                    observer.close()
                client.close_test_stdio()


if __name__ == "__main__":
    run_this_suite(__file__)
