"""Fixture FIFOs, markers, and causal test gates owned by the worker."""

import json
import os
from pathlib import Path


TEST_FIXTURE_CONTROL_ENV = "ZOD_TEST_FIXTURE_CONTROL"
TEST_EVENT_FIFO_NAME = "zod-test-events"
TEST_CONTROL_FIFO_NAME = "zod-test-control"
TEST_CLEANUP_FIFO_NAME = "zod-test-cleanup"
TEST_RESPONSE_QUERY_FIFO_NAME = "zod-test-response-query"
TEST_RESPONSE_RESULT_FIFO_NAME = "zod-test-response-result"
TEST_CONTROL_READY_NAME = "zod-test-control-ready"
test_event_descriptor: int | None = None
test_control_descriptor: int | None = None
test_cleanup_descriptor: int | None = None
test_response_query_descriptor: int | None = None
test_response_result_descriptor: int | None = None


def configure_test_fixture_control(temporary: Path) -> None:
    global test_event_descriptor
    global test_control_descriptor
    global test_cleanup_descriptor
    global test_response_query_descriptor
    global test_response_result_descriptor

    if os.environ.get(TEST_FIXTURE_CONTROL_ENV) != "1":
        return
    event_path = temporary / TEST_EVENT_FIFO_NAME
    control_path = temporary / TEST_CONTROL_FIFO_NAME
    cleanup_path = temporary / TEST_CLEANUP_FIFO_NAME
    paths = [event_path, control_path, cleanup_path]
    response_gate_enabled = "ZOD_TEST_RESPONSE_GATE_RELEASED" in os.environ
    if response_gate_enabled:
        paths.extend(
            [
                temporary / TEST_RESPONSE_QUERY_FIFO_NAME,
                temporary / TEST_RESPONSE_RESULT_FIFO_NAME,
            ]
        )
    for path in paths:
        os.mkfifo(path)
    test_event_descriptor = os.open(event_path, os.O_RDWR)
    test_control_descriptor = os.open(control_path, os.O_RDWR)
    test_cleanup_descriptor = os.open(cleanup_path, os.O_RDONLY | os.O_NONBLOCK)
    if response_gate_enabled:
        test_response_query_descriptor = os.open(
            temporary / TEST_RESPONSE_QUERY_FIFO_NAME,
            os.O_RDWR,
        )
        test_response_result_descriptor = os.open(
            temporary / TEST_RESPONSE_RESULT_FIFO_NAME,
            os.O_RDWR,
        )
    publish_marker(temporary / TEST_CONTROL_READY_NAME)


def emit_test_event(operation: int, kind: str, **details: object) -> None:
    if test_event_descriptor is None:
        return
    event = {
        "operation": operation,
        "kind": kind,
        "component": "fixture",
        **details,
    }
    payload = json.dumps(event, separators=(",", ":")).encode() + b"\n"
    assert len(payload) <= 512
    assert os.write(test_event_descriptor, payload) == len(payload)


def publish_marker(path: Path, contents: str | None = None) -> None:
    pending = path.with_name(f".{path.name}.{os.getpid()}.pending")
    if contents is None:
        pending.touch()
    else:
        pending.write_text(contents, encoding="utf-8")
    pending.replace(path)


def wait_for_test_control(operation: int, kind: str) -> dict[str, object]:
    assert test_control_descriptor is not None
    payload = bytearray()
    while not payload.endswith(b"\n"):
        chunk = os.read(test_control_descriptor, 1)
        assert chunk, f"test control closed before {kind!r} for request {operation}"
        payload.extend(chunk)
    command = json.loads(payload)
    assert command.get("operation") == operation, command
    assert command.get("kind") == kind, command
    return command


def duplicate_test_cleanup_gate() -> int:
    assert test_cleanup_descriptor is not None
    duplicate = os.dup(test_cleanup_descriptor)
    os.set_blocking(duplicate, True)
    return duplicate


def close_test_cleanup_gate() -> None:
    global test_cleanup_descriptor
    assert test_cleanup_descriptor is not None
    os.close(test_cleanup_descriptor)
    test_cleanup_descriptor = None


def close_test_control_channel() -> None:
    global test_control_descriptor
    assert test_control_descriptor is not None
    os.close(test_control_descriptor)
    test_control_descriptor = None


def response_gate_completed() -> bool:
    assert test_response_query_descriptor is not None
    assert test_response_result_descriptor is not None
    assert os.write(test_response_query_descriptor, b"1") == 1
    completed = os.read(test_response_result_descriptor, 1)
    assert completed in {b"0", b"1"}, completed
    return completed == b"1"
