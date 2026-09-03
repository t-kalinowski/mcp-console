# /// script
# requires-python = ">=3.11"
# dependencies = ["py-yaml12"]
# ///

import array
import base64
import fcntl
import json
import os
import plistlib
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import termios
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Self

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import (
    FifoCheckpoint,
    McpClient,
    Transcript,
    TranscriptWithCompanions,
    assert_result_content,
    build_manager_interposer,
    build_r_input_handler,
    checkpoint_uv_environment,
    code,
    collect_running_output,
    normalize_python_resolution_error,
    normalize_python_traceback_paths,
    r_test_environment,
    reference_plots,
    release_worker_callback_gate,
    run_this_suite,
    stop_client,
    wait_for_evaluation_output,
    wait_for_idle_output,
    wait_for_worker_file,
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


class ZodFixtureControl:
    def __init__(self, root: Path | None = None) -> None:
        self.temporary_directory = (
            tempfile.TemporaryDirectory() if root is None else None
        )
        if root is None:
            assert self.temporary_directory is not None
            root = Path(self.temporary_directory.name)
        self.root = root
        self.event_reader: int | None = None
        self.control_writer: int | None = None
        self.cleanup_writer: int | None = None
        self.events: list[dict[str, object]] = []
        self.buffer = bytearray()
        self.cleanup_released = False

    def configure(self, environment: dict[str, str]) -> None:
        environment["TMPDIR"] = str(self.root)
        environment["ZOD_TEST_FIXTURE_CONTROL"] = "1"

    def connect(self, client: McpClient) -> None:
        if self.event_reader is not None:
            return
        directory = wait_for_marker(
            self.root,
            TEST_CONTROL_READY_NAME,
            client,
        ).parent
        event_reader = os.open(
            directory / TEST_EVENT_FIFO_NAME,
            os.O_RDONLY | os.O_NONBLOCK,
        )
        control_writer = os.open(
            directory / TEST_CONTROL_FIFO_NAME,
            os.O_WRONLY | os.O_NONBLOCK,
        )
        cleanup_writer = os.open(
            directory / TEST_CLEANUP_FIFO_NAME,
            os.O_WRONLY | os.O_NONBLOCK,
        )
        os.set_blocking(control_writer, True)
        os.set_blocking(cleanup_writer, True)
        self.event_reader = event_reader
        self.control_writer = control_writer
        self.cleanup_writer = cleanup_writer

    def send_control(self, operation: int, kind: str, **details: object) -> None:
        assert self.control_writer is not None
        payload = (
            json.dumps(
                {"operation": operation, "kind": kind, **details},
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        assert os.write(self.control_writer, payload) == len(payload)

    def release_cleanup(self) -> None:
        if self.cleanup_released:
            return
        if self.cleanup_writer is not None:
            try:
                os.write(self.cleanup_writer, b"1")
            except BrokenPipeError:
                pass
            os.close(self.cleanup_writer)
            self.cleanup_writer = None
        self.cleanup_released = True

    def wait_for(self, operation: int, kind: str) -> dict[str, object]:
        return self.wait_for_any(operation, {kind})

    def wait_for_any(
        self,
        operation: int,
        kinds: set[str],
    ) -> dict[str, object]:
        deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
        while True:
            event = next(
                (
                    event
                    for event in self.events
                    if event.get("operation") == operation
                    and event.get("kind") in kinds
                ),
                None,
            )
            if event is not None:
                return event
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"Zod did not emit one of {sorted(kinds)!r} for request "
                    f"{operation}; " + self.diagnostics()
                )
            assert self.event_reader is not None
            readable, _, _ = select.select([self.event_reader], [], [], remaining)
            if not readable:
                continue
            chunk = os.read(self.event_reader, 4096)
            assert chunk, "Zod event channel closed; " + self.diagnostics()
            self.record_events(chunk)

    def record_events(self, chunk: bytes) -> None:
        self.buffer.extend(chunk)
        while b"\n" in self.buffer:
            line, _, remainder = self.buffer.partition(b"\n")
            self.buffer = bytearray(remainder)
            event = json.loads(line)
            assert isinstance(event, dict), event
            assert set(event) >= {"operation", "kind", "component"}, event
            self.events.append(event)

    def assert_before(
        self,
        first: tuple[int, str],
        second: tuple[int, str],
    ) -> None:
        positions = {
            (event["operation"], event["kind"]): index
            for index, event in enumerate(self.events)
        }
        assert positions[first] < positions[second], self.diagnostics()

    def record_client_event(self, operation: int, kind: str, **details: object) -> None:
        self.events.append(
            {
                "operation": operation,
                "kind": kind,
                "component": "client",
                **details,
            }
        )

    def diagnostics(self) -> str:
        started = {
            event["operation"]
            for event in self.events
            if event.get("kind") == "worker_operation_started"
        }
        completed = {
            event["operation"]
            for event in self.events
            if event.get("kind") == "worker_operation_completed"
        }
        trace = "\n".join(json.dumps(event, sort_keys=True) for event in self.events)
        return f"outstanding requests: {sorted(started - completed)}; event trace:\n{trace}"

    def wait_for_eof(self) -> None:
        assert self.event_reader is not None
        deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    "Zod event channel remained open after fixture cleanup; "
                    + self.diagnostics()
                )
            readable, _, _ = select.select([self.event_reader], [], [], remaining)
            if not readable:
                continue
            chunk = os.read(self.event_reader, 4096)
            if not chunk:
                assert not self.buffer, self.diagnostics()
                return
            self.record_events(chunk)

    def close(self) -> None:
        self.release_cleanup()
        if self.control_writer is not None:
            os.close(self.control_writer)
        if self.event_reader is not None:
            os.close(self.event_reader)
        if self.temporary_directory is not None:
            self.temporary_directory.cleanup()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()


class ResponseGateObserver:
    def __init__(
        self,
        root: Path,
        stream: socket.socket,
        release: Path,
    ) -> None:
        self.root = root
        self.stream = stream
        self.release = release
        self.stop_requested = threading.Event()
        self.error: Exception | None = None
        self.responded = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        query_reader: int | None = None
        result_writer: int | None = None
        try:
            deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
            while True:
                ready = list(self.root.glob(f"**/{TEST_CONTROL_READY_NAME}"))
                assert len(ready) <= 1, (
                    f"found multiple Zod fixture control channels: {ready!r}"
                )
                if ready:
                    directory = ready[0].parent
                    break
                if self.stop_requested.wait(0.01):
                    return
                assert time.monotonic() < deadline, (
                    "Zod did not create its response gate controls"
                )

            query_reader = os.open(
                directory / TEST_RESPONSE_QUERY_FIFO_NAME,
                os.O_RDONLY | os.O_NONBLOCK,
            )
            result_writer = os.open(
                directory / TEST_RESPONSE_RESULT_FIFO_NAME,
                os.O_WRONLY | os.O_NONBLOCK,
            )
            os.set_blocking(result_writer, True)
            while True:
                try:
                    query = os.read(query_reader, 1)
                    break
                except BlockingIOError:
                    if self.stop_requested.wait(0.01):
                        return
            assert query == b"1", query
            completed = self.release.is_file()
            if not completed:
                try:
                    queued = self.stream.recv(
                        64 * 1024,
                        socket.MSG_PEEK | socket.MSG_DONTWAIT,
                    )
                    completed = b"\n" in queued
                except BlockingIOError:
                    pass
            assert os.write(result_writer, b"1" if completed else b"0") == 1
            self.responded = True
        except Exception as error:
            self.error = error
        finally:
            if query_reader is not None:
                os.close(query_reader)
            if result_writer is not None:
                os.close(result_writer)

    def finish(self) -> None:
        self.thread.join(FIXTURE_CHECKPOINT_TIMEOUT_SECONDS)
        assert not self.thread.is_alive(), "Zod did not query the response gate"
        if self.error is not None:
            raise self.error
        assert self.responded

    def close(self) -> None:
        self.stop_requested.set()
        self.thread.join(1)
        assert not self.thread.is_alive(), "response gate observer did not stop"


def queued_socket_bytes(stream: socket.socket) -> int:
    available = array.array("i", [0])
    fcntl.ioctl(stream.fileno(), termios.FIONREAD, available, True)
    return available[0]


class SocketTextReader:
    def __init__(self, stream: socket.socket) -> None:
        self.stream = stream
        self.buffer = bytearray()

    def wait_for_incomplete_response(
        self,
        request: int,
        minimum_complete_size: int,
        diagnostics: str,
    ) -> int:
        assert not self.buffer
        readable, _, _ = select.select(
            [self.stream],
            [],
            [],
            FIXTURE_CHECKPOINT_TIMEOUT_SECONDS,
        )
        assert readable, (
            f"response {request} did not reach the test gate; {diagnostics}"
        )
        pending = queued_socket_bytes(self.stream)
        assert 0 < pending < minimum_complete_size, (
            f"response {request} completed before the test gate; "
            f"buffered={pending}; minimum_complete_size={minimum_complete_size}; "
            f"{diagnostics}"
        )
        prefix = self.stream.recv(min(pending, 256), socket.MSG_PEEK)
        assert f'"id":{request},'.encode() in prefix, prefix
        assert b"\n" not in prefix, prefix
        return pending

    def release_completed_response(
        self,
        request: int,
        release: Path,
        diagnostics: str,
    ) -> None:
        deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, (
                f"response {request} did not complete at the test gate; {diagnostics}"
            )
            readable, _, _ = select.select([self.stream], [], [], remaining)
            assert readable, (
                f"response {request} did not advance at the test gate; {diagnostics}"
            )
            pending = queued_socket_bytes(self.stream)
            assert pending > 0, (
                f"response stream closed at the test gate for {request}; {diagnostics}"
            )
            queued = self.stream.recv(pending, socket.MSG_PEEK)
            if b"\n" in queued:
                release.touch()
                return
            self.buffer.extend(self.stream.recv(pending))

    def readline(self) -> str:
        while b"\n" not in self.buffer:
            chunk = self.stream.recv(64 * 1024)
            if not chunk:
                data = bytes(self.buffer)
                self.buffer.clear()
                return data.decode()
            self.buffer.extend(chunk)
        line, _, remainder = self.buffer.partition(b"\n")
        self.buffer = bytearray(remainder)
        return (line + b"\n").decode()

    def read(self) -> str:
        chunks = [bytes(self.buffer)]
        self.buffer.clear()
        while chunk := self.stream.recv(64 * 1024):
            chunks.append(chunk)
        return b"".join(chunks).decode()

    def close(self) -> None:
        self.stream.close()


class SocketGateMcpClient(McpClient):
    def __init__(
        self,
        binary: Path,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        current_directory: Path,
    ) -> None:
        input_writer, input_reader = socket.socketpair()
        output_reader, output_writer = socket.socketpair()
        output_writer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024)
        output_reader.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024)
        self.output_buffer_sizes = (
            output_writer.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF),
            output_reader.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
        )
        assert max(self.output_buffer_sizes) < TEST_GATED_RESPONSE_SIZE
        process = subprocess.Popen(
            [binary, *arguments],
            env=environment,
            cwd=current_directory,
            stdin=input_reader,
            stdout=output_writer,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        output_writer.close()
        input_stream = input_writer.makefile("w", encoding="utf-8")
        input_writer.close()
        assert process.stderr is not None

        self.temporary_directory = None
        self.process = process
        self.stdin = input_stream
        self.stdout = SocketTextReader(output_reader)
        self.stderr = process.stderr
        self.transcript = []
        self._next_request_id = 1
        self._issued_request_ids = set()
        self.input_reader = input_reader
        self.test_stdio_closed = False

    def wait_until_input_is_read(
        self,
        description: str,
        control: ZodFixtureControl,
    ) -> None:
        """Wait for the server's next read from the staged input socket.

        Tests write exactly one complete JSONL frame at a time. A later frame is
        withheld until this returns, so observing that later frame leave the
        socket proves the preceding frame passed through ServerTransport.receive.
        """
        deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
        while queued_socket_bytes(self.input_reader) != 0:
            assert self.process.poll() is None, (
                f"mcp-console stopped before consuming {description}; "
                + control.diagnostics()
            )
            assert time.monotonic() < deadline, (
                f"mcp-console did not consume {description}; " + control.diagnostics()
            )
            time.sleep(0.001)

    def close_input_observer(self) -> None:
        if self.input_reader.fileno() != -1:
            self.input_reader.close()

    def close_test_stdio(self) -> None:
        if self.test_stdio_closed:
            return
        self.close_input_observer()
        self.stdout.close()
        self.test_stdio_closed = True


def build_killpg_denial_interposer(directory: Path) -> Path:
    source = directory / "deny-killpg.c"
    library = directory / "deny-killpg.dylib"
    source.write_text(
        r"""
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>

static void write_pid_marker(const char *name, pid_t process_id) {
    const char *marker = getenv(name);
    if (marker == NULL) {
        return;
    }
    int descriptor = open(marker, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (descriptor >= 0) {
        dprintf(descriptor, "%d\n", process_id);
        close(descriptor);
    }
}

static int deny_killpg(pid_t process_group, int signal) {
    if (signal == SIGINT
        && getenv("MCP_CONSOLE_TEST_DENIED_SIGINT") != NULL) {
        write_pid_marker("MCP_CONSOLE_TEST_DENIED_SIGINT", process_group);
        errno = EPERM;
        return -1;
    }
    return (int)syscall(SYS_kill, -process_group, signal);
}

__attribute__((constructor))
static void remove_interposer_from_child_environment(void) {
    unsetenv("DYLD_INSERT_LIBRARIES");
}

__attribute__((used))
static struct {
    const void *replacement;
    const void *replacee;
} interposers[] __attribute__((section("__DATA,__interpose"))) = {
    {(const void *)&deny_killpg, (const void *)&killpg},
};
""".removeprefix("\n"),
        encoding="utf-8",
    )
    subprocess.run(
        ["cc", "-dynamiclib", "-o", library, source],
        check=True,
        capture_output=True,
        text=True,
    )
    return library


def record_resolved_r_library(environment: dict[str, str], directory: Path) -> None:
    real_ir = shutil.which("ir", path=environment.get("PATH"))
    assert real_ir is not None, "ir is required"
    identity = directory / "resolved-r-library"
    fake_bin = directory / "fixture-r-bin"
    fake_bin.mkdir()
    ir = fake_bin / "ir"
    ir.write_text(
        code(r"""
            #!/bin/sh

            set -eu
            if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
              exec "$MCP_CONSOLE_TEST_REAL_IR" "$@"
            fi
            if [ -n "${MCP_CONSOLE_TEST_R_RESOLUTION_FAILURE:-}" ] &&
              [ -e "$MCP_CONSOLE_TEST_R_RESOLUTION_FAILURE" ]; then
              printf 'fixture R resolver failed\n' >&2
              exit 1
            fi
            library=$("$MCP_CONSOLE_TEST_REAL_IR" "$@")
            printf '%s' "$library" > "$MCP_CONSOLE_TEST_R_LIBRARY_IDENTITY"
            printf '%s' "$library"
            """),
        encoding="utf-8",
    )
    ir.chmod(0o755)
    path = environment.get("PATH")
    assert path is not None, "PATH is required"
    environment["PATH"] = os.pathsep.join((str(fake_bin), path))
    environment["MCP_CONSOLE_TEST_REAL_IR"] = real_ir
    environment["MCP_CONSOLE_TEST_R_LIBRARY_IDENTITY"] = str(identity)


def expose_idle_input_request(client: McpClient, temporary_path: Path) -> None:
    requested = client._start_send(r="request input while idle")
    completed = wait_for_marker(
        temporary_path,
        "zod-idle-input-cell-completed",
        client,
    )
    client._receive(requested)
    assert last_tool_text(client) == "[done]"

    release_fixture_checkpoint(completed.parent / "zod-release-idle-input-request")
    wait_for_marker(
        temporary_path,
        "zod-idle-input-request-processed",
        client,
    )
    client.send()
    assert last_tool_text(client) == (
        '[input requested: "idle> "]\n[waiting for stdin]'
    )


def resolver_interrupt_permission_environment(
    temporary_path: Path,
) -> tuple[dict[str, str], FifoCheckpoint, FifoCheckpoint, Path, Path]:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    fake_bin = temporary_path / "bin"
    fake_bin.mkdir()
    fake_ir = fake_bin / "ir"
    fake_ir.write_text(
        code(r"""
            #!/bin/sh

            set -eu
            if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
              printf 'ir 0.4.0\n'
              exit 0
            fi
            exec 3< "$MCP_CONSOLE_TEST_RESOLVER_LIFETIME"
            printf '%s\n' "$$" > "$MCP_CONSOLE_TEST_RESOLVER_GROUP"
            printf 1 > "$MCP_CONSOLE_TEST_RESOLVER_STARTED"
            IFS= read -r _ <&3
            """),
        encoding="utf-8",
    )
    fake_ir.chmod(0o755)

    path = environment.get("PATH")
    assert path is not None, "PATH is required"
    environment["PATH"] = os.pathsep.join((str(fake_bin), path))
    environment["TMPDIR"] = str(temporary_path)
    denied_interrupt = temporary_path / "resolver-sigint-denied"
    resolver_group = temporary_path / "resolver-group"
    resolver_started = FifoCheckpoint(temporary_path / "resolver-started")
    resolver_lifetime = FifoCheckpoint(temporary_path / "resolver-lifetime")
    environment["MCP_CONSOLE_TEST_DENIED_SIGINT"] = str(denied_interrupt)
    environment["MCP_CONSOLE_TEST_RESOLVER_GROUP"] = str(resolver_group)
    environment["MCP_CONSOLE_TEST_RESOLVER_STARTED"] = str(resolver_started.path)
    environment["MCP_CONSOLE_TEST_RESOLVER_LIFETIME"] = str(resolver_lifetime.path)
    # The interposer removes its loader variable after reaching the server, so
    # the resolver and Zod do not inherit it.
    environment["DYLD_INSERT_LIBRARIES"] = str(
        build_killpg_denial_interposer(temporary_path)
    )
    return (
        environment,
        resolver_started,
        resolver_lifetime,
        resolver_group,
        denied_interrupt,
    )


def submit_prompted_stdin(
    client: McpClient,
    temporary_path: Path,
    stdin: str,
    marker: str,
    expected: str,
) -> None:
    poll_start = len(client.transcript)
    submitted = client._start_send(stdin=stdin)
    wait_for_marker(temporary_path, marker, client)
    client._receive(submitted)
    if last_tool_text(client) != expected:
        assert last_tool_text(client) == "\n[waiting for stdin]"
        client.send()
    assert last_tool_text(client) == expected
    calls = client.transcript[poll_start:]
    submitted["result"] = calls[-1]["result"]
    client.transcript[poll_start:] = [submitted]


def _zod_last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    return result["content"][0]["text"]


def assert_large_output(output: str, prefix: str) -> None:
    expected = prefix + ("x" * LARGE_OUTPUT_SIZE)
    assert output.startswith(expected), (
        f"captured {len(output)} bytes without the complete {len(expected)}-byte payload"
    )
    barrier = output.removeprefix(expected)
    assert barrier and not barrier.strip("y"), "unexpected text after captured payload"


def large_output(prefix: str) -> str:
    return prefix + ("x" * LARGE_OUTPUT_SIZE) + ("y" * LARGE_OUTPUT_SIZE)


def remove_length_marker(output: str, marker_prefix: str) -> tuple[str, int]:
    marker_start = output.find(marker_prefix)
    assert marker_start >= 0, (
        f"raw output lost length marker {marker_prefix!r}: {output[-500:]!r}"
    )
    marker_end = output.find("\n", marker_start)
    if marker_end < 0:
        marker_end = len(output)
        after_marker = marker_end
    else:
        after_marker = marker_end + 1
    length = int(output[marker_start + len(marker_prefix) : marker_end])
    return output[:marker_start] + output[after_marker:], length


def expose_idle_sideband_output(
    client: McpClient,
    temporary_path: Path,
    marker: str | None = None,
) -> None:
    suffix = f"-{marker}" if marker else ""
    source = (
        f"start background sideband: {marker}"
        if marker
        else "start background sideband"
    )
    client.send(r=source)
    assert last_tool_text(client) == "[done]", repr(last_tool_text(client))
    started = wait_for_marker(
        temporary_path,
        f"zod-background-sideband-started{suffix}",
        client,
    )
    (started.parent / f"zod-release-background-sideband{suffix}").touch()
    wait_for_marker(
        temporary_path,
        f"zod-background-sideband-emitted{suffix}",
        client,
    )


def wait_for_marker(root: Path, name: str, client: McpClient) -> Path:
    deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
    events = select.kqueue()
    directories: dict[Path, int] = {}
    try:
        events.control(
            [
                select.kevent(
                    client.process.pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_EXIT,
                )
            ],
            0,
            0,
        )
        while True:
            marker = find_marker(root, name)
            if marker is not None:
                return marker

            watch_marker_directories(root, events, directories)
            marker = find_marker(root, name)
            if marker is not None:
                return marker
            assert client.process.poll() is None, (
                f"mcp-console stopped before Zod reported its {name!r} checkpoint"
            )

            remaining = deadline - time.monotonic()
            assert remaining > 0, (
                f"Zod did not report its {name!r} checkpoint within "
                f"{FIXTURE_CHECKPOINT_TIMEOUT_SECONDS} seconds"
            )
            try:
                observed = events.control(
                    None,
                    max(1, len(directories) + 1),
                    remaining,
                )
            except InterruptedError:
                continue
            assert observed, (
                f"Zod did not report its {name!r} checkpoint within "
                f"{FIXTURE_CHECKPOINT_TIMEOUT_SECONDS} seconds"
            )
    finally:
        for descriptor in directories.values():
            os.close(descriptor)
        events.close()


def find_marker(root: Path, name: str) -> Path | None:
    markers = [path for path in [root / name] if path.exists()]
    markers.extend(root.glob(f"mcp-console-tmp-*/{name}"))
    assert len(markers) <= 1, f"found multiple {name} markers"
    return markers[0] if markers else None


def watch_marker_directories(
    root: Path,
    events: select.kqueue,
    directories: dict[Path, int],
) -> None:
    def watch(directory: Path) -> None:
        if directory in directories:
            return
        try:
            descriptor = os.open(directory, os.O_EVTONLY | os.O_CLOEXEC)
        except FileNotFoundError:
            return
        events.control(
            [
                select.kevent(
                    descriptor,
                    filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=(
                        select.KQ_NOTE_WRITE
                        | select.KQ_NOTE_RENAME
                        | select.KQ_NOTE_DELETE
                        | select.KQ_NOTE_REVOKE
                    ),
                )
            ],
            0,
            0,
        )
        directories[directory] = descriptor

    # Watch the root before discovering private temporary directories. This
    # makes creation of a directory after the glob snapshot observable.
    watch(root)
    for directory in root.glob("mcp-console-tmp-*"):
        if directory.is_dir():
            watch(directory)


def wait_for_stopped_worker(
    root: Path,
    previous_process_ids: set[int],
    recorded_workers: list[tuple[int, int]],
    client: McpClient,
) -> tuple[Path, int, int]:
    deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
    while True:
        for marker in root.glob("mcp-console-tmp-*/zod-stop-continue-worker"):
            try:
                contents = marker.read_text(encoding="utf-8")
            except FileNotFoundError:
                # Restart may remove the old generation's directory between
                # enumeration and opening while this waits for its replacement.
                continue
            process_id, parent_id, process_group = map(
                int,
                contents.split(),
            )
            if process_id in previous_process_ids:
                continue
            worker = (process_id, process_group)
            if worker not in recorded_workers:
                recorded_workers.append(worker)
            assert parent_id == process_group, (
                "stopped worker is not the relay's direct child"
            )
            assert process_id != process_group, (
                "stopped worker unexpectedly leads the relay process group"
            )
            assert process_group != os.getpgrp(), (
                "stopped worker shares the test process group"
            )
            status = read_process_status(process_id)
            if status is not None and status[2].startswith("T"):
                assert status[:2] == (parent_id, process_group), (
                    "stopped worker changed its process boundary"
                )
                return marker, process_id, process_group
        assert client.process.poll() is None, (
            "mcp-console stopped before its direct worker reached SIGSTOP"
        )
        assert time.monotonic() < deadline, (
            "direct worker did not enter the stopped process state within "
            f"{FIXTURE_CHECKPOINT_TIMEOUT_SECONDS} seconds"
        )
        time.sleep(0.01)


def wait_for_stopped_process(
    process_id: int,
    process_group: int,
    client: McpClient,
    description: str,
) -> None:
    deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
    while True:
        status = read_process_status(process_id)
        assert status is not None, (
            f"{description} process exited before reaching SIGSTOP"
        )
        assert status[1] == process_group, f"{description} process changed groups"
        if status[2].startswith("T"):
            return
        assert client.process.poll() is None, (
            f"mcp-console stopped before {description} reached SIGSTOP"
        )
        assert time.monotonic() < deadline, (
            f"{description} did not reach SIGSTOP within "
            f"{FIXTURE_CHECKPOINT_TIMEOUT_SECONDS} seconds"
        )
        time.sleep(0.01)


def wait_for_path(path: Path, description: str, client: McpClient) -> None:
    observed = wait_for_marker(path.parent, path.name, client)
    assert observed == path, f"found a different path while waiting for {description}"


def read_process_status(process_id: int) -> tuple[int, int, str] | None:
    status = subprocess.run(
        [
            "ps",
            "-o",
            "ppid=",
            "-o",
            "pgid=",
            "-o",
            "state=",
            "-p",
            str(process_id),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if status.returncode == 1 and not status.stdout.strip():
        return None
    assert status.returncode == 0, status.stderr
    fields = status.stdout.split()
    assert len(fields) == 3, status.stdout
    return int(fields[0]), int(fields[1]), fields[2]


def continue_stopped_worker(process_id: int, process_group: int) -> None:
    status = read_process_status(process_id)
    assert status is not None, "stopped worker exited before SIGCONT"
    assert status[1] == process_group, "stopped worker changed process groups"
    assert status[2].startswith("T"), "worker was not stopped before SIGCONT"
    os.kill(process_id, signal.SIGCONT)


def wait_for_worker_retirement(
    process_id: int,
    process_group: int,
    client: McpClient,
) -> None:
    deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
    while read_process_status(process_id) is not None or process_group_exists(
        process_group
    ):
        assert client.process.poll() is None, (
            "mcp-console stopped while retiring the old worker generation"
        )
        assert time.monotonic() < deadline, (
            "restart did not retire the old worker and relay process group"
        )
        time.sleep(0.01)


def stop_recorded_worker(process_id: int, process_group: int) -> None:
    assert process_group != os.getpgrp(), "refusing to stop the test process group"
    stop_process_group(process_group)
    status = read_process_status(process_id)
    if status is not None and status[1] == process_group:
        stop_process_id(process_id)


def read_worker_group(marker: Path) -> int:
    worker_group = int(marker.read_text(encoding="utf-8"))
    assert worker_group != os.getpgrp(), "Zod did not enter a dedicated process group"
    return worker_group


def release_partial_sideband(marker: Path) -> None:
    release = marker.with_name("zod-release-partial-sideband")
    with release.open("wb", buffering=0) as stream:
        assert stream.write(b"x") == 1


def release_fixture_checkpoint(path: Path) -> None:
    with path.open("wb", buffering=0) as stream:
        assert stream.write(b"1") == 1


def process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_process_id(process_id: int | None) -> None:
    if process_id is None:
        return
    try:
        os.kill(process_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def wait_for_process_group_exit(process_group: int, client: McpClient) -> None:
    deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
    while process_group_exists(process_group):
        assert client.process.poll() is None, "mcp-console stopped during restart"
        assert time.monotonic() < deadline, (
            "restart did not enforce its shutdown deadline"
        )
        time.sleep(0.01)


def stop_process_group(process_group: int | None) -> None:
    if process_group is None:
        return
    assert process_group > 0, process_group
    assert process_group != os.getpgrp(), process_group
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


PYTHON_DOWNLOAD_URL = "https://example.invalid/python.tar.zst"


def named_requirement_error(requirement: str) -> str:
    return (
        f"Python requirement `{requirement}` is not accepted: host-side managed "
        "resolution accepts named package requirements only"
    )


def python_version_constraint_error(constraint: str) -> str:
    return (
        f"Python version constraint `{constraint}` is not accepted: host-side managed "
        "resolution accepts version numbers and supported PEP 440 version specifiers only"
    )


def normalize_duckdb_resolution_error(error: str, extension: str) -> str:
    detail = next(
        line.strip().removeprefix("! ")
        for line in error.splitlines()
        if f'Failed to download extension "{extension}"' in line
    )
    return detail.partition(' at URL "')[0]


def ir_cache_directory(environment: dict[str, str]) -> str:
    ir = shutil.which("ir", path=environment.get("PATH"))
    assert ir is not None, "ir is required"
    cache = subprocess.run(
        [ir, "cache", "dir"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()
    assert cache and Path(cache).is_absolute(), (
        f"ir returned invalid cache directory: {cache}"
    )
    return cache


def matplotlib_test_environment(cache_home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    cache = ir_cache_directory(environment)
    environment["IR_CACHE_DIR"] = cache
    environment["XDG_CACHE_HOME"] = str(cache_home)
    assert ir_cache_directory(environment) == cache
    return environment


def python_inventory_client(
    binary: Path,
    directory: Path,
    *,
    preference: str | None = None,
    install_directory: Path | None = None,
    resolver_python: Path | None = None,
    resolver_record: Path | None = None,
    extra_environment: dict[str, str] | None = None,
) -> tuple[McpClient, Path, Path]:
    real_uv = shutil.which("uv")
    assert real_uv is not None, "real uv is required"
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment.pop("UV_PYTHON_PREFERENCE", None)
    environment["RETICULATE_UV"] = str(
        Path(__file__).parents[2] / "fixtures" / "record_uv_environment"
    )
    environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
    environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(directory / "uv.jsonl")
    arguments = directory / "uv-arguments.jsonl"
    environment["MCP_CONSOLE_TEST_UV_ARGUMENTS_RECORD"] = str(arguments)
    inventories = directory / "uv-python-inventories.json"
    environment["MCP_CONSOLE_TEST_UV_PYTHON_INVENTORIES"] = str(inventories)
    if preference is not None:
        environment["UV_PYTHON_PREFERENCE"] = preference
    if install_directory is not None:
        environment["UV_PYTHON_INSTALL_DIR"] = str(install_directory)
    if resolver_python is not None:
        environment["MCP_CONSOLE_TEST_UV_PYTHON"] = str(resolver_python)
    if resolver_record is not None:
        environment["MCP_CONSOLE_TEST_UV_RESOLVER_RECORD"] = str(resolver_record)
    if extra_environment is not None:
        environment.update(extra_environment)
    client = McpClient(
        binary,
        ("serve",),
        environment,
        current_directory=directory,
    )
    client._initialize_and_list_tools()
    arguments.write_text("", encoding="utf-8")
    if resolver_record is not None:
        resolver_record.write_text("", encoding="utf-8")
    return client, inventories, arguments


def uv_python_row(
    version: str,
    *,
    path: str | Path | None = None,
    url: str | None = PYTHON_DOWNLOAD_URL,
    variant: str = "default",
    implementation: str = "cpython",
) -> dict[str, object]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    assert match is not None, version
    major, minor, patch = (int(part) for part in match.groups())
    return {
        "key": f"{implementation}-{version}-macos-aarch64-none",
        "version": version,
        "version_parts": {"major": major, "minor": minor, "patch": patch},
        "path": None if path is None else str(path),
        "symlink": None,
        "url": url,
        "variant": variant,
        "implementation": implementation,
    }


def write_uv_python_inventories(path: Path, inventories: dict[str, object]) -> None:
    path.write_text(json.dumps(inventories), encoding="utf-8")


def recorded_python_preferences(arguments: Path) -> list[str]:
    invocations = [
        json.loads(line) for line in arguments.read_text(encoding="utf-8").splitlines()
    ]
    return [
        invocation[invocation.index("--python-preference") + 1]
        for invocation in invocations
        if invocation[:2] == ["python", "list"]
    ]


def recorded_tool_run_pythons(arguments: Path) -> list[str]:
    invocations = [
        json.loads(line) for line in arguments.read_text(encoding="utf-8").splitlines()
    ]
    return [
        invocation[invocation.index("--python") + 1]
        for invocation in invocations
        if invocation[:2] == ["tool", "run"] and "--python" in invocation
    ]


def read_uv_resolver_records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def resolve_public_python_version(
    client: McpClient,
    constraints: list[str],
) -> str:
    constraints_r = (
        "character()"
        if not constraints
        else f"c({', '.join(json.dumps(value) for value in constraints)})"
    )
    # fmt: r
    r = code(rf"""
        reticulate::py_require(
          python_version = {
            constraints_r
          },
          action = "set"
        )
        result <- tryCatch(
          reticulate::py_write_requirements(
            NULL,
            NULL,
            freeze = FALSE,
            python = NULL
          )$python_version,
          error = conditionMessage
        )
        cat(result, "\n", sep = "")
        """)
    client.send(r=r)
    return last_tool_text(client)


def write_python_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def managed_python_transcript(binary: Path, configured: bool) -> Transcript:
    environment = os.environ.copy()
    if configured:
        environment["RETICULATE_PYTHON"] = "managed"
    else:
        environment.pop("RETICULATE_PYTHON", None)
    uv = shutil.which("uv")
    assert uv is not None, "real uv is required for managed-Python tests"
    environment.pop("RETICULATE_UV", None)
    environment["UV_OFFLINE"] = "1"

    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        python <- Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)
        config <- reticulate::py_config()
        history <- reticulate::py_require()$history
        stopifnot(
          identical(python, "managed"),
          file.exists(config$python),
          isTRUE(config$ephemeral),
          "pandas" %in% reticulate::py_require()$packages,
          !any(vapply(
            history,
            function(request) identical(request$requested_from, "base"),
            logical(1L)
          ))
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]", client.transcript[-1]
    # fmt: python
    python = code("""
        import io
        import pandas as pd

        frame = pd.read_csv(io.StringIO("value\\n40\\n2\\n"))
        int(frame["value"].sum())
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert output == "42\n", repr(output)
    return client._finish()


def _python_last_tool_text(client: McpClient) -> str:
    return client.transcript[-1]["result"]["content"][0]["text"]


def assert_exact_interleaving(actual: str, first: str, second: str) -> None:
    assert len(actual) == len(first) + len(second), repr(actual)
    first_offsets = {0}
    for offset, character in enumerate(actual):
        next_offsets = set()
        for first_offset in first_offsets:
            second_offset = offset - first_offset
            if first_offset < len(first) and first[first_offset] == character:
                next_offsets.add(first_offset + 1)
            if second_offset < len(second) and second[second_offset] == character:
                next_offsets.add(first_offset)
        first_offsets = next_offsets
    assert len(first) in first_offsets, repr(actual)


@contextmanager
def r_input_handler_client(binary: Path) -> Iterator[tuple[McpClient, Path]]:
    with tempfile.TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        environment, rscript = r_test_environment()
        environment["TMPDIR"] = temporary_directory
        build_r_input_handler(directory, environment, rscript)
        client = McpClient(
            binary,
            ("serve",),
            environment=environment,
            current_directory=directory,
        )
        try:
            yield client, directory
        finally:
            stop_client(client)


def _r_last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    return result["content"][0]["text"]


PENDING_TEXT_BUDGET = 8 * 1024 * 1024


def recording_uv_environment(
    directory: Path,
    *,
    fail_requirement: str | None = None,
    substitute_requirement: tuple[str, str] | None = None,
) -> tuple[dict[str, str], Path]:
    real_uv = shutil.which("uv")
    assert real_uv is not None, "real uv is required"
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment["RETICULATE_UV"] = str(
        Path(__file__).resolve().parents[2] / "fixtures" / "record_uv_environment"
    )
    environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
    environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(directory / "uv-environment.jsonl")
    arguments_record = directory / "uv-arguments.jsonl"
    environment["MCP_CONSOLE_TEST_UV_ARGUMENTS_RECORD"] = str(arguments_record)
    if fail_requirement is not None:
        failure_marker = directory / "uv-failure"
        failure_marker.touch()
        environment["MCP_CONSOLE_TEST_UV_FAILURE_MARKER"] = str(failure_marker)
        environment["MCP_CONSOLE_TEST_UV_FAILURE_ARGUMENT"] = fail_requirement
    if substitute_requirement is not None:
        substitute, replacement = substitute_requirement
        environment["MCP_CONSOLE_TEST_UV_SUBSTITUTE_REQUIREMENT"] = substitute
        environment["MCP_CONSOLE_TEST_UV_REPLACEMENT_REQUIREMENT"] = replacement
    return environment, arguments_record


def uv_tool_run_requirements(record: Path) -> list[list[str]]:
    if not record.exists():
        return []
    arguments = [
        json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()
    ]
    requirements = []
    for invocation in arguments:
        if invocation[:2] != ["tool", "run"]:
            continue
        separator = invocation.index("--")
        manifest = [
            invocation[index + 1]
            for index, argument in enumerate(invocation[:separator])
            if argument == "--with"
        ]
        requirements.append(manifest)
    return requirements


def initialize_python_and_record_baseline(client: McpClient, record: Path) -> int:
    client.send(python="None")
    assert last_tool_text(client) == "[done]"
    return len(uv_tool_run_requirements(record))


def resolve_managed_python(binary: Path, directory: Path) -> Path:
    workspace = directory / "managed-python"
    workspace.mkdir()
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment.pop("UV_PYTHON", None)
    client = McpClient(
        binary,
        ("serve",),
        environment,
        current_directory=workspace,
    )
    client._initialize_and_list_tools()
    client.send(python='import sys\nprint(f"managed-python={sys.executable}")')
    output = last_tool_text(client)
    client._finish()
    executable = Path(
        next(
            line for line in output.splitlines() if line.startswith("managed-python=")
        ).split("=", 1)[1]
    ).resolve()
    assert executable.is_file(), executable
    return executable


def send_and_collect_runtime_python_resolution(
    client: McpClient,
    **arguments: object,
) -> str:
    call_start = len(client.transcript)
    client.send(**arguments)
    chunks = []
    for attempt in range(8):
        output = last_tool_text(client)
        if output.endswith("\n[running; poll with an empty send]"):
            chunks.append(output.removesuffix("\n[running; poll with an empty send]"))
            if attempt == 7:
                raise AssertionError(
                    "automatic Python resolution remained running after eight "
                    f"responses: collected={''.join(chunks)!r}, last={output!r}"
                )
            client.send(timeout_ms=30_000)
            continue

        if output != "[done]" or not chunks:
            chunks.append(output)
        collected = "".join(chunks)

        calls = client.transcript[call_start:]
        submitted = calls[0]
        final_result = calls[-1]["result"]
        content = final_result["content"]
        assert len(content) == 1 and content[0]["type"] == "text", content
        content[0]["text"] = collected
        submitted["result"] = final_result
        client.transcript[call_start:] = [submitted]
        return collected
    raise AssertionError("unreachable")


def last_tool_text(client: McpClient) -> str:
    return client.transcript[-1]["result"]["content"][0]["text"]


def last_tool_text_from_entry(entry: dict[str, object]) -> str:
    result = entry["result"]
    assert isinstance(result, dict), result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    return content[0]["text"]
