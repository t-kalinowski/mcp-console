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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import (
    FifoCheckpoint,
    McpClient,
    Transcript,
    TranscriptWithCompanions,
    code,
    r_test_environment,
    run_this_suite,
    stop_client,
)

PLATFORMS = {"darwin"}
LARGE_OUTPUT_SIZE = 2 * 1024 * 1024
PENDING_TEXT_BUDGET = 8 * 1024 * 1024
TEST_GATED_RESPONSE_SIZE = 128 * 1024
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)


class ZodFixtureControl:
    def __init__(self) -> None:
        self.event_reader, self.event_writer = os.pipe()
        self.control_reader, self.control_writer = os.pipe()
        self.cleanup_reader, self.cleanup_writer = os.pipe()
        os.set_blocking(self.event_reader, False)
        self.events: list[dict[str, object]] = []
        self.buffer = bytearray()
        self.child_ends_closed = False
        self.cleanup_released = False

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return (self.event_writer, self.control_reader, self.cleanup_reader)

    def configure(self, environment: dict[str, str]) -> None:
        environment["ZOD_TEST_EVENT_FD"] = str(self.event_writer)
        environment["ZOD_TEST_CONTROL_FD"] = str(self.control_reader)
        environment["ZOD_TEST_FIXTURE_CLEANUP_FD"] = str(self.cleanup_reader)

    def close_child_ends(self) -> None:
        assert not self.child_ends_closed
        os.close(self.event_writer)
        os.close(self.control_reader)
        os.close(self.cleanup_reader)
        self.child_ends_closed = True

    def send_control(self, operation: int, kind: str, **details: object) -> None:
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
        try:
            os.write(self.cleanup_writer, b"1")
        except BrokenPipeError:
            pass
        os.close(self.cleanup_writer)
        self.cleanup_released = True

    def wait_for(self, operation: int, kind: str) -> dict[str, object]:
        return self.wait_for_any(operation, {kind})

    def wait_for_any(
        self,
        operation: int,
        kinds: set[str],
    ) -> dict[str, object]:
        deadline = time.monotonic() + 15
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
            readable, _, _ = select.select([self.event_reader], [], [], remaining)
            if not readable:
                continue
            chunk = os.read(self.event_reader, 4096)
            assert chunk, "Zod event pipe closed; " + self.diagnostics()
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
        deadline = time.monotonic() + 15
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    "Zod event pipe remained open after fixture cleanup; "
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
        os.close(self.control_writer)
        if not self.cleanup_released:
            os.close(self.cleanup_writer)
        if not self.child_ends_closed:
            os.close(self.event_writer)
            os.close(self.control_reader)
            os.close(self.cleanup_reader)
        os.close(self.event_reader)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()


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
        readable, _, _ = select.select([self.stream], [], [], 15)
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
        deadline = time.monotonic() + 15
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
        pass_fds: tuple[int, ...],
    ) -> None:
        input_writer, input_reader = socket.socketpair()
        output_reader, output_writer = socket.socketpair()
        output_observer = output_reader.dup()
        output_writer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024)
        output_reader.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024)
        self.output_buffer_sizes = (
            output_writer.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF),
            output_reader.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
        )
        assert max(self.output_buffer_sizes) < TEST_GATED_RESPONSE_SIZE
        environment["ZOD_TEST_RESPONSE_SOCKET_FD"] = str(output_observer.fileno())
        process = subprocess.Popen(
            [binary, *arguments],
            env=environment,
            cwd=current_directory,
            stdin=input_reader,
            stdout=output_writer,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            pass_fds=(*pass_fds, output_observer.fileno()),
        )
        output_observer.close()
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
        deadline = time.monotonic() + 15
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
#include <sys/wait.h>
#include <sys/types.h>
#include <unistd.h>

static pid_t denied_process_group = 0;
static int added_late_member = 0;
static pid_t late_member = 0;
static int killpg_count = 0;

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

static void write_member_marker(pid_t process_id, pid_t process_group) {
    const char *marker = getenv("MCP_CONSOLE_TEST_LATE_MEMBER_MARKER");
    if (marker == NULL) {
        return;
    }
    int descriptor = open(marker, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (descriptor >= 0) {
        dprintf(descriptor, "%d %d\n", process_id, process_group);
        close(descriptor);
    }
}

static int deny_killpg(pid_t process_group, int signal) {
    if (signal == SIGKILL
        && getenv("MCP_CONSOLE_TEST_KILLPG_COUNT_MARKER") != NULL) {
        const char *marker = getenv("MCP_CONSOLE_TEST_KILLPG_COUNT_MARKER");
        int descriptor = open(marker, O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (descriptor >= 0) {
            killpg_count += 1;
            dprintf(descriptor, "%d %d\n", killpg_count, process_group);
            close(descriptor);
        }
    }
    if (signal == SIGKILL
        && getenv("MCP_CONSOLE_TEST_KILLPG_MARKER") != NULL) {
        denied_process_group = process_group;
        write_pid_marker("MCP_CONSOLE_TEST_KILLPG_MARKER", process_group);
        errno = EPERM;
        return -1;
    }
    if (signal == SIGINT
        && getenv("MCP_CONSOLE_TEST_DENIED_SIGINT") != NULL) {
        write_pid_marker("MCP_CONSOLE_TEST_DENIED_SIGINT", process_group);
        errno = EPERM;
        return -1;
    }
    return (int)syscall(SYS_kill, -process_group, signal);
}

static pid_t add_process_group_member(pid_t process_group) {
    int descriptors[2];
    if (pipe(descriptors) != 0) {
        return -1;
    }

    pid_t member = fork();
    if (member < 0) {
        close(descriptors[0]);
        close(descriptors[1]);
        return -1;
    }
    if (member == 0) {
        close(descriptors[0]);
        if (setpgid(0, process_group) != 0) {
            _exit(1);
        }
        pid_t process_id = getpid();
        if (write(descriptors[1], &process_id, sizeof(process_id))
            != sizeof(process_id)) {
            _exit(1);
        }
        close(descriptors[1]);
        for (;;) {
            pause();
        }
    }

    close(descriptors[1]);
    pid_t acknowledged_member = 0;
    ssize_t bytes_read;
    do {
        bytes_read = read(
            descriptors[0],
            &acknowledged_member,
            sizeof(acknowledged_member)
        );
    } while (bytes_read < 0 && errno == EINTR);
    int read_error = bytes_read < 0 ? errno : EIO;
    close(descriptors[0]);

    if (bytes_read != sizeof(acknowledged_member)
        || acknowledged_member != member) {
        syscall(SYS_kill, member, SIGKILL);
        while (waitpid(member, NULL, 0) < 0 && errno == EINTR) {
        }
        errno = read_error;
        return -1;
    }
    return member;
}

static pid_t getpgid_and_add_member(pid_t process_id) {
    pid_t process_group = (pid_t)syscall(SYS_getpgid, process_id);
    // Rust rechecks group membership only after taking its kernel snapshot.
    // Join the group here so a one-pass fallback cannot observe this child.
    if (process_group == denied_process_group && !added_late_member) {
        added_late_member = 1;
        pid_t member = add_process_group_member(process_group);
        if (member < 0) {
            return -1;
        }
        late_member = member;
        write_member_marker(member, process_group);
    }
    return process_group;
}

static int kill_and_reap_late_member(pid_t process_id, int signal) {
    int result = (int)syscall(SYS_kill, process_id, signal);
    int signal_error = errno;
    if (result == 0 && signal == SIGKILL && process_id == late_member) {
        // Keep the final assertion independent of launchd's orphan reaping.
        int status = 0;
        pid_t waited;
        do {
            waited = waitpid(process_id, &status, 0);
        } while (waited < 0 && errno == EINTR);
        if (waited != process_id) {
            return -1;
        }
        write_pid_marker("MCP_CONSOLE_TEST_LATE_MEMBER_REAP_MARKER", process_id);
        late_member = 0;
    }
    errno = signal_error;
    return result;
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
    {(const void *)&getpgid_and_add_member, (const void *)&getpgid},
    {(const void *)&kill_and_reap_late_member, (const void *)&kill},
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


def build_transcript_projection_failure_interposer(directory: Path) -> Path:
    source = directory / "fail-transcript-projection.c"
    library = directory / "fail-transcript-projection.dylib"
    source.write_text(
        r"""
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <unistd.h>

static int is_target(const char *path) {
    const char *target = getenv("MCP_CONSOLE_TEST_PROJECTION_TARGET");
    if (target == NULL) {
        return 0;
    }
    const char *name = strrchr(path, '/');
    name = name == NULL ? path : name + 1;
    return strcmp(name, target) == 0;
}

static int fail_creation(const char *path) {
    const char *failure = getenv("MCP_CONSOLE_TEST_PROJECTION_FAILURE");
    return failure != NULL
        && strcmp(failure, "create") == 0
        && is_target(path);
}

static int fail_append(int descriptor) {
    const char *failure = getenv("MCP_CONSOLE_TEST_PROJECTION_FAILURE");
    if (failure == NULL || strcmp(failure, "append") != 0) {
        return 0;
    }
    char path[PATH_MAX];
    return fcntl(descriptor, F_GETPATH, path) == 0 && is_target(path);
}

static int projection_open(const char *path, int flags, ...) {
    mode_t mode = 0;
    if ((flags & O_CREAT) != 0) {
        va_list arguments;
        va_start(arguments, flags);
        mode = va_arg(arguments, int);
        va_end(arguments);
    }
    if (fail_creation(path)) {
        errno = EACCES;
        return -1;
    }
    return (int)syscall(SYS_open, path, flags, mode);
}

static int projection_openat(int directory, const char *path, int flags, ...) {
    mode_t mode = 0;
    if ((flags & O_CREAT) != 0) {
        va_list arguments;
        va_start(arguments, flags);
        mode = va_arg(arguments, int);
        va_end(arguments);
    }
    if (fail_creation(path)) {
        errno = EACCES;
        return -1;
    }
    return (int)syscall(SYS_openat, directory, path, flags, mode);
}

static ssize_t projection_write(
    int descriptor,
    const void *buffer,
    size_t length
) {
    if (fail_append(descriptor)) {
        errno = EIO;
        return -1;
    }
    return (ssize_t)syscall(SYS_write, descriptor, buffer, length);
}

static ssize_t projection_writev(
    int descriptor,
    const struct iovec *buffers,
    int count
) {
    if (fail_append(descriptor)) {
        errno = EIO;
        return -1;
    }
    return (ssize_t)syscall(SYS_writev, descriptor, buffers, count);
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
    {(const void *)&projection_open, (const void *)&open},
    {(const void *)&projection_openat, (const void *)&openat},
    {(const void *)&projection_write, (const void *)&write},
    {(const void *)&projection_writev, (const void *)&writev},
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


def test_routes_send_over_sideband(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(r="echo hello")
    assert last_tool_text(client) == "zod: hello\n"
    client.send(python="echo precise 👩🏽‍💻")
    assert last_tool_text(client) == "zod python: precise 👩🏽‍💻\n"
    client.send(sql="echo two  spaces")
    assert last_tool_text(client) == "zod sql: two  spaces\n"
    return client._finish()


def test_projects_console_kinds(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    result = client.send(r="emit console kinds")
    assert result == {
        "content": [
            {
                "type": "text",
                "text": "zod output\nzod diagnostic\n",
            }
        ],
        "isError": False,
    }, result
    return client._finish()


def test_returns_worker_images(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(r="emit image")
    result = client.transcript[-1]["result"]
    assert result == {
        "content": [
            {"type": "text", "text": "before image\n"},
            {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
            {"type": "text", "text": "after image\n"},
        ],
        "isError": False,
    }, result
    return client._finish()


def test_materializes_records_only_for_console_use(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        unused_workspace = temporary / "unused"
        unused_workspace.mkdir()
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            {**os.environ, "TMPDIR": str(unused_workspace)},
            current_directory=unused_workspace,
        )
        client._initialize_and_list_tools()
        assert not (unused_workspace / ".mcp-console").exists(), unused_workspace
        removed = client._request(
            "tools/call",
            name="session",
            arguments={"action": "restart"},
        )
        assert removed["error"] == {
            "code": -32602,
            "message": "tool not found",
        }, removed
        assert not (unused_workspace / ".mcp-console").exists(), unused_workspace
        assert not list(unused_workspace.glob("mcp-console-tmp-*")), unused_workspace
        transcript = client._finish()
        assert not (unused_workspace / ".mcp-console").exists(), unused_workspace

        workspace = temporary / "send"
        workspace.mkdir()
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        assert not (workspace / ".mcp-console").exists(), workspace
        client.send(r="echo echo")

        sessions = list((workspace / ".mcp-console" / "sessions").iterdir())
        assert len(sessions) == 1, sessions
        events = [
            json.loads(line)
            for line in (sessions[0] / "internal" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert [event["event"] for event in events] == [
            "session_started",
            "tool_call",
            "tool_result",
        ], events
        assert events[1]["request"]["name"] == "send", events[1]
        client._finish()

        transcript.append(
            {
                "recording": {
                    "initialization and removed session tool only": "absent",
                    "materialized by": {"send": [event["event"] for event in events]},
                }
            }
        )
        return transcript


def test_continues_without_record_when_record_cannot_be_created(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        (workspace / ".mcp-console").write_text("occupied", encoding="utf-8")
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            current_directory=workspace,
        )
        client._initialize_and_list_tools()

        client.send(r="echo echo")
        client.send(control="restart")

        client._request("tools/call", name="missing", arguments={})
        assert client.transcript[-1]["error"] == {
            "code": -32602,
            "message": "tool not found",
        }, client.transcript[-1]
        assert (workspace / ".mcp-console").read_text(encoding="utf-8") == "occupied"
        transcript, standard_error = client._finish_with_standard_error()
        assert standard_error.count("\n") == 1, standard_error
        assert standard_error.startswith(
            "mcp-console: transcript recording disabled: failed to create "
        ), standard_error
        assert ".mcp-console/sessions" in standard_error, standard_error
        transcript.append(
            {
                "server stderr": (
                    "mcp-console: transcript recording disabled: "
                    "<run record creation failed>"
                )
            }
        )
        return transcript


def run_derived_projection_failure(
    binary: Path,
    *,
    failure: str,
    target: str,
    surviving_projection: str,
    injected_failure: str | None = None,
    injected_target: str | None = None,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        workspace = temporary / "workspace"
        workspace.mkdir()
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            build_transcript_projection_failure_interposer(temporary)
        )
        environment["MCP_CONSOLE_TEST_PROJECTION_FAILURE"] = injected_failure or failure
        environment["MCP_CONSOLE_TEST_PROJECTION_TARGET"] = injected_target or target
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
            current_directory=workspace,
        )
        client._initialize_and_list_tools()

        client.send(r="emit image")
        client.send(python="echo after projection failure")

        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        journal = session / "internal" / "events.jsonl"
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        assert [event["event"] for event in events] == [
            "session_started",
            "tool_call",
            "artifact_created",
            "tool_result",
            "tool_call",
            "tool_result",
        ], events
        assert [event["sequence"] for event in events] == list(range(1, 7)), events
        artifact = events[2]
        assert (session / artifact["path"]).read_bytes() == base64.b64decode(PNG_1X1)

        surviving_text = (session / surviving_projection).read_text(encoding="utf-8")
        if surviving_projection == "transcript.qmd":
            assert "```{r}\nemit image\n```" in surviving_text, surviving_text
            assert (
                "```{python}\necho after projection failure\n```" in surviving_text
            ), surviving_text
        else:
            assert "[Artifact 1 from call 1]" in surviving_text, surviving_text
            assert "![Artifact 1]" in surviving_text, surviving_text
            assert "echo after projection failure" in surviving_text, surviving_text

        transcript, standard_error = client._finish_with_standard_error()
        assert standard_error.count("\n") == 1, standard_error
        assert standard_error.startswith(
            "mcp-console: transcript projection disabled: "
        ), standard_error
        projection_description = (
            target
            if failure == "create"
            else injected_target
            or {
                "transcript.md": "Markdown transcript",
                "transcript.qmd": "Quarto source transcript",
            }[target]
        )
        assert projection_description in standard_error, standard_error
        assert "transcript recording disabled" not in standard_error, standard_error
        transcript.append(
            {
                "derived projection failure": {
                    "failure": failure,
                    "target": target,
                    "journal events": [event["event"] for event in events],
                    "artifact recorded": True,
                    "surviving projection": surviving_projection,
                    "server stderr": (
                        "mcp-console: transcript projection disabled: "
                        "<projection failure>"
                    ),
                }
            }
        )
        return transcript


def test_keeps_recording_when_markdown_creation_fails(binary: Path) -> Transcript:
    return run_derived_projection_failure(
        binary,
        failure="create",
        target="transcript.md",
        surviving_projection="transcript.qmd",
    )


def test_keeps_recording_when_quarto_rewrite_fails(binary: Path) -> Transcript:
    return run_derived_projection_failure(
        binary,
        failure="rewrite",
        target="transcript.qmd",
        surviving_projection="transcript.md",
        injected_failure="append",
        injected_target=".transcript.qmd.tmp",
    )


def test_updates_quarto_without_rereading_journal(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            current_directory=workspace,
        )
        finished = False
        journal_read_disabled = False
        try:
            client._initialize_and_list_tools()
            client.send(r="echo first")

            session = next((workspace / ".mcp-console" / "sessions").iterdir())
            journal = session / "internal" / "events.jsonl"
            journal.chmod(0o200)
            journal_read_disabled = True

            client.send(python="echo second")
            quarto = (session / "transcript.qmd").read_text(encoding="utf-8")

            journal.chmod(0o600)
            journal_read_disabled = False
            assert "```{r}\necho first\n```" in quarto, quarto
            assert "```{python}\necho second\n```" in quarto, quarto

            transcript = client._finish()
            transcript.append(
                {
                    "quarto projection": {
                        "updated from incremental state": True,
                        "journal reopened for reading": False,
                    }
                }
            )
            finished = True
            return transcript
        finally:
            if journal_read_disabled:
                journal.chmod(0o600)
            if not finished:
                stop_client(client)


def test_records_tool_calls_and_images(binary: Path) -> TranscriptWithCompanions:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        environment, _ = r_test_environment()
        environment["RETICULATE_PYTHON"] = ""
        record_resolved_r_library(environment, workspace)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
            current_directory=workspace,
            umask=0,
        )
        client._initialize_and_list_tools()
        client.send(
            r="emit image",
            stdin="recorded stdin\n",
            requirements={"r": ["praise"]},
        )
        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        quarto_path = session / "transcript.qmd"
        quarto_before_python_requirement = quarto_path.read_text(encoding="utf-8")
        quarto_before_inode = quarto_path.stat().st_ino
        assert "    - praise" in quarto_before_python_requirement
        assert "transcript-fixture" not in quarto_before_python_requirement
        image_request_id = client.transcript[-1]["id"]
        invalid = client._request(
            "tools/call",
            name="send",
            arguments={"r": "1", "python": "1"},
            _meta={"progressToken": "record-me"},
        )
        client.send(requirements={"python": ["transcript-fixture"]})
        preparation_request_id = client.transcript[-1]["id"]
        preparation_result = client.transcript[-1]["result"]
        client._request("tools/call", name="missing", arguments={})

        sessions = list((workspace / ".mcp-console" / "sessions").iterdir())
        assert len(sessions) == 1, sessions
        session = sessions[0]
        journal_text = (session / "internal" / "events.jsonl").read_text(
            encoding="utf-8"
        )
        markdown_path = session / "transcript.md"
        markdown_text = markdown_path.read_text(encoding="utf-8")
        quarto_text = quarto_path.read_text(encoding="utf-8")
        assert PNG_1X1 not in journal_text, journal_text
        assert PNG_1X1 not in markdown_text, markdown_text
        assert PNG_1X1 not in quarto_text, quarto_text
        events = [json.loads(line) for line in journal_text.splitlines()]
        assert [event["event"] for event in events] == [
            "session_started",
            "tool_call",
            "artifact_created",
            "tool_result",
            "tool_call",
            "tool_result",
            "tool_call",
            "tool_result",
        ], events
        run_id = events[0]["run_id"]
        assert run_id
        assert session.name == run_id, (session, run_id)
        assert events[0]["session"] == "default", events[0]
        assert Path(events[0]["working_directory"]).samefile(workspace), events[0]
        assert all(event["run_id"] == run_id for event in events), events
        assert all(event["schema_version"] == 1 for event in events), events
        assert [event["sequence"] for event in events] == list(range(1, 9)), events
        assert events[1]["call_id"] == events[2]["call_id"] == 1, events
        assert events[1]["request_id"] == image_request_id, events[1]
        assert events[1]["request"] == {
            "name": "send",
            "arguments": {
                "r": "emit image",
                "stdin": "recorded stdin\n",
                "requirements": {"r": ["praise"]},
            },
        }, events[1]
        assert {
            key: events[2][key]
            for key in ("artifact_id", "call_id", "path", "mime_type", "bytes")
        } == {
            "artifact_id": 1,
            "call_id": 1,
            "path": "artifacts/call-000001-image-000001.png",
            "mime_type": "image/png",
            "bytes": len(base64.b64decode(PNG_1X1)),
        }, events[2]
        assert events[3]["result"] == {
            "content": [
                {"type": "text", "text": "before image\n"},
                {
                    "type": "image",
                    "artifactId": 1,
                    "path": "artifacts/call-000001-image-000001.png",
                    "mimeType": "image/png",
                },
                {"type": "text", "text": "after image\n"},
            ],
            "isError": False,
        }, events[3]
        assert events[4]["call_id"] == events[5]["call_id"] == 2, events
        assert events[4]["request_id"] == invalid["id"], events[4]
        assert events[4]["request"] == {
            "name": "send",
            "arguments": {"r": "1", "python": "1"},
            "_meta": {"progressToken": "record-me"},
        }, events[4]
        assert events[5]["result"] == {
            "content": [
                {
                    "type": "text",
                    "text": "only one of `r`, `python`, or `sql` may be supplied",
                }
            ],
            "isError": True,
        }, events[5]
        assert events[6]["call_id"] == events[7]["call_id"] == 3, events
        assert events[6]["request_id"] == preparation_request_id, events[6]
        assert events[6]["request"] == {
            "name": "send",
            "arguments": {
                "requirements": {"python": ["transcript-fixture"]},
            },
        }, events[6]
        assert events[7]["result"] == preparation_result, events[7]
        assert [event["request"]["name"] for event in events if "request" in event] == [
            "send",
            "send",
            "send",
        ], events
        assert all(
            event.get("request", {}).get("name") != "missing" for event in events
        ), events

        image_path = session / events[3]["result"]["content"][1]["path"]
        image_bytes = image_path.read_bytes()
        assert image_bytes == base64.b64decode(PNG_1X1), image_path
        directory_modes = {
            path.relative_to(workspace).as_posix(): path.stat().st_mode & 0o777
            for path in (
                workspace / ".mcp-console",
                workspace / ".mcp-console" / "sessions",
                session,
                session / "artifacts",
                session / "internal",
            )
        }
        assert set(directory_modes.values()) == {0o700}, directory_modes
        file_modes = {
            path.relative_to(workspace).as_posix(): path.stat().st_mode & 0o777
            for path in (
                session / "internal" / "events.jsonl",
                markdown_path,
                quarto_path,
                image_path,
            )
        }
        assert set(file_modes.values()) == {0o600}, file_modes
        transcript = client._finish()

        timestamps = [event["at"] for event in events]
        working_directory = events[0]["working_directory"]
        for event in events:
            assert event["at"].endswith("Z"), event
            datetime.fromisoformat(event["at"])
            event["at"] = "<UTC timestamp>"
            event["run_id"] = "<run ID>"
            if "request_id" in event:
                event["request_id"] = "<request ID>"
        events[0]["working_directory"] = "<workspace>"
        assert journal_text.endswith("\n"), journal_text
        assert markdown_text.endswith("\n"), markdown_text
        assert quarto_text.endswith("\n"), quarto_text
        markdown_text = markdown_text.replace(run_id, "<run ID>")
        markdown_text = markdown_text.replace(working_directory, "<workspace>")
        quarto_text = quarto_text.replace(working_directory, "<workspace>")
        for timestamp in timestamps:
            markdown_text = markdown_text.replace(timestamp, "<UTC timestamp>")
        assert "```{r}\nemit image\n```" in quarto_text
        assert quarto_path.stat().st_ino != quarto_before_inode
        assert "    - praise" in quarto_text
        assert "    - transcript-fixture" in quarto_text
        assert "python-version:" not in quarto_text
        assert all(
            excluded not in quarto_text
            for excluded in (
                "recorded stdin",
                "before image",
                "Artifact 1",
                "Result for call",
            )
        ), quarto_text

        return TranscriptWithCompanions(
            transcript=transcript,
            companions={
                "events.yaml": [
                    events,
                    {"transcript.md": markdown_text.splitlines()},
                    {"transcript.qmd": quarto_text.splitlines()},
                    {
                        "produced session": {
                            "root": ".mcp-console/sessions/<run ID>",
                            "files": [
                                "internal/events.jsonl",
                                "transcript.md",
                                "transcript.qmd",
                                "artifacts/call-000001-image-000001.png",
                            ],
                        }
                    },
                ],
            },
        )


def test_quotes_quarto_fences(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        source = "echo before\n````\n<div>not markdown</div>\nafter"
        client.send(python=source)
        rejected_source = "echo retained despite invalid option"
        rejected = client._request(
            "tools/call",
            name="send",
            arguments={"python": rejected_source, "typo": True},
        )
        assert rejected["result"]["isError"] is True, rejected

        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        markdown = (session / "transcript.md").read_text(encoding="utf-8")
        quarto = (session / "transcript.qmd").read_text(encoding="utf-8")
        assert f"`````python\n{source}\n`````" in markdown
        assert (
            "`````text\nzod python: before\n````\n<div>not markdown</div>\nafter\n`````"
        ) in markdown
        assert f"```python\n{rejected_source}\n```" in markdown
        assert '"typo": true' in markdown
        assert f"`````{{python}}\n{source}\n`````" in quarto
        assert f"```{{python}}\n{rejected_source}\n```" in quarto
        assert "zod python:" not in quarto

        transcript = client._finish()
        transcript.append(
            {
                "markdown projection": {
                    "source fence exceeds literal backtick run": True,
                    "result fence exceeds literal backtick run": True,
                    "source from a rejected call is retained": True,
                    "raw rejected request is retained": True,
                },
                "quarto projection": {
                    "source fence exceeds literal backtick run": True,
                    "source from a rejected call is retained": True,
                    "results are omitted": True,
                },
            }
        )
        return transcript


def test_disables_recording_after_transcript_failure(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        client.send(r="echo echo")
        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        artifacts = session / "artifacts"
        artifacts.rmdir()
        artifacts.write_text("not a directory", encoding="utf-8")

        client.send(r="emit image")
        image_result = client.transcript[-1]["result"]
        assert image_result == {
            "content": [
                {"type": "text", "text": "before image\n"},
                {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                {"type": "text", "text": "after image\n"},
            ],
            "isError": False,
        }, image_result

        journal = session / "internal" / "events.jsonl"
        journal_after_failure = journal.read_text(encoding="utf-8")
        events = [json.loads(line) for line in journal_after_failure.splitlines()]
        assert [event["event"] for event in events] == [
            "session_started",
            "tool_call",
            "tool_result",
            "tool_call",
        ], events
        assert journal_after_failure.endswith("\n"), journal_after_failure

        client.send(r="echo echo")
        assert journal.read_text(encoding="utf-8") == journal_after_failure

        transcript, standard_error = client._finish_with_standard_error()
        assert standard_error.count("\n") == 1, standard_error
        assert standard_error.startswith(
            "mcp-console: transcript recording disabled: failed to create "
        ), standard_error
        assert "/artifacts/" in standard_error, standard_error
        transcript.append(
            {
                "journal after failure": [event["event"] for event in events],
                "complete final line": True,
                "post-failure append": False,
                "server stderr": (
                    "mcp-console: transcript recording disabled: "
                    "<artifact persistence failed>"
                ),
            }
        )
        return transcript


def test_flushes_calls_and_keeps_unpolled_images(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        workspace = temporary / "workspace"
        workspace.mkdir()
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
            current_directory=workspace,
        )
        client._initialize_and_list_tools()

        waiting = client._start_send(
            r="complete after release",
            timeout_ms=3_000,
        )
        started = wait_for_marker(
            temporary,
            "zod-evaluation-started",
            client,
        )
        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        journal = session / "internal" / "events.jsonl"
        markdown = session / "transcript.md"
        quarto = session / "transcript.qmd"
        before_release = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        assert [event["event"] for event in before_release] == [
            "session_started",
            "tool_call",
        ], before_release
        before_release_markdown = markdown.read_text(encoding="utf-8")
        before_release_quarto = quarto.read_text(encoding="utf-8")
        assert "## Call 1: R" in before_release_markdown
        assert "complete after release" in before_release_markdown
        assert "## Result for call 1" not in before_release_markdown
        assert "```{r}\ncomplete after release\n```" in before_release_quarto
        markdown_inode = markdown.stat().st_ino
        quarto_inode = quarto.stat().st_ino

        (started.parent / "zod-release-evaluation").touch()
        client._receive(waiting)
        after_release = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        assert [event["event"] for event in after_release] == [
            "session_started",
            "tool_call",
            "tool_result",
        ], after_release
        after_release_markdown = markdown.read_text(encoding="utf-8")
        after_release_quarto = quarto.read_text(encoding="utf-8")
        assert after_release_markdown.startswith(before_release_markdown)
        assert markdown.stat().st_ino == markdown_inode
        assert "## Result for call 1" in after_release_markdown
        assert "zod: complete after release" in after_release_markdown
        assert after_release_quarto == before_release_quarto
        assert quarto.stat().st_ino == quarto_inode

        client.send(
            r="emit image before completion",
            timeout_ms=0,
        )
        assert client.transcript[-1]["result"] == {
            "content": [
                {"type": "text", "text": "\n[running; poll with an empty send]"}
            ],
            "isError": False,
        }, client.transcript[-1]
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "<leading newline>[running; poll with an empty send]"
        )
        image_started = wait_for_marker(
            temporary,
            "zod-image-evaluation-started",
            client,
        )
        (image_started.parent / "zod-release-image").touch()
        wait_for_marker(temporary, "zod-image-processed", client)

        final_events = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        assert [event["event"] for event in final_events] == [
            "session_started",
            "tool_call",
            "tool_result",
            "tool_call",
            "tool_result",
            "artifact_created",
        ], final_events
        artifact = final_events[-1]
        assert {
            key: artifact[key]
            for key in ("artifact_id", "call_id", "path", "mime_type", "bytes")
        } == {
            "artifact_id": 1,
            "call_id": 2,
            "path": "artifacts/call-000002-image-000001.png",
            "mime_type": "image/png",
            "bytes": len(base64.b64decode(PNG_1X1)),
        }, artifact
        image_path = session / artifact["path"]
        assert image_path.read_bytes() == base64.b64decode(PNG_1X1), image_path
        unpolled_markdown = markdown.read_text(encoding="utf-8")
        unpolled_quarto = quarto.read_text(encoding="utf-8")
        assert unpolled_markdown.startswith(after_release_markdown)
        assert markdown.stat().st_ino == markdown_inode
        assert f"[Artifact {artifact['artifact_id']} from call 2]" in unpolled_markdown
        assert artifact["path"] in unpolled_markdown
        assert unpolled_quarto.startswith(after_release_quarto)
        assert "```{r}\nemit image before completion\n```" in unpolled_quarto
        assert quarto.stat().st_ino != quarto_inode
        unpolled_quarto_inode = quarto.stat().st_ino

        (image_started.parent / "zod-release-image-completion").touch()
        client.send(timeout_ms=3_000)
        poll_result = client.transcript[-1]["result"]
        assert poll_result == {
            "content": [{"type": "image", "data": PNG_1X1, "mimeType": "image/png"}],
            "isError": False,
        }, poll_result
        polled_events = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        assert [event["event"] for event in polled_events[-2:]] == [
            "tool_call",
            "tool_result",
        ], polled_events
        assert polled_events[-1]["call_id"] == 3, polled_events[-1]
        assert polled_events[-1]["result"] == {
            "content": [
                {
                    "type": "image",
                    "mimeType": "image/png",
                    "artifactId": artifact["artifact_id"],
                    "path": artifact["path"],
                }
            ],
            "isError": False,
        }, polled_events[-1]
        polled_markdown = markdown.read_text(encoding="utf-8")
        polled_quarto = quarto.read_text(encoding="utf-8")
        assert polled_markdown.startswith(unpolled_markdown)
        assert markdown.stat().st_ino == markdown_inode
        assert "## Call 3: Poll" in polled_markdown
        assert "## Result for call 3" in polled_markdown
        assert polled_quarto == unpolled_quarto
        assert quarto.stat().st_ino == unpolled_quarto_inode

        transcript = client._finish()
        transcript.append(
            {
                "live journal": {
                    "while first call was running": [
                        event["event"] for event in before_release
                    ],
                    "after first call completed": [
                        event["event"] for event in after_release
                    ],
                    "unpolled image": {
                        "event": artifact["event"],
                        "path": artifact["path"],
                        "data": "<byte-identical decoded PNG>",
                    },
                    "later poll result": polled_events[-1]["result"],
                    "Markdown projection": {
                        "live before result": True,
                        "each snapshot retained as an exact prefix": True,
                        "inode retained": True,
                    },
                    "Quarto projection": "source cells only",
                }
            }
        )
        return transcript


def test_custom_worker_skips_managed_python_preflight(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment["R_HOME"] = "/mcp-console-custom-worker-must-not-run-rscript"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
        environment,
    )
    client._initialize_and_list_tools()
    # fmt: python
    python = code(r"""
        echo echo
        """).removesuffix("\n")
    client.send(python=python)
    result = client.send(requirements={"python": ["py-yaml12"]})
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "Python requirements are unavailable with a custom worker"
    ), result
    result = client.send(
        control="restart",
        requirements={"python": ["py-yaml12"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "Python requirements are unavailable with a custom worker"
    ), result
    result = client.send(
        r="echo must not run",
        requirements={"python": ["py-yaml12"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "Python requirements are unavailable with a custom worker"
    )
    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"
    client.send()
    assert last_tool_text(client) == "\n[idle]"
    return client._finish()


def test_standalone_preparation_before_worker_startup_is_causal_and_idempotent(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    relay = Path(__file__).resolve().parents[2] / "fixtures" / "scripted_relay"
    ir = Path(__file__).resolve().parents[2] / "fixtures" / "ordered_retirement_ir"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        library = temporary / "standalone-candidate"
        library.mkdir()
        fake_bin = temporary / "bin"
        fake_bin.mkdir()
        (fake_bin / "ir").symlink_to(ir)
        resolver_started = FifoCheckpoint(temporary / "resolver-started")
        resolver_release = FifoCheckpoint(temporary / "resolver-release")
        worker_started = temporary / "zod-started"
        resolver_counter = temporary / "ir-counter"
        environment, _ = r_test_environment()
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_IR_COUNTER"] = str(resolver_counter)
        environment["MCP_CONSOLE_TEST_IR_LIBRARIES"] = str(library)
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        environment["MCP_CONSOLE_TEST_RELAY_SCENARIO"] = "ready"
        environment["MCP_CONSOLE_TEST_ZOD_STARTED"] = str(worker_started)
        client = McpClient(
            binary,
            (
                "serve",
                "--worker",
                str(zod),
                "--relay",
                str(relay),
            ),
            environment,
        )
        finished = False
        released = False
        try:
            client._initialize_and_list_tools()
            invalid = client._start_send(
                requirements={"r": ["must-not-resolve"]},
                stdin="must not queue\n",
            )
            readable, _, _ = select.select(
                [client.stdout, resolver_started.descriptor],
                [],
                [],
                10,
            )
            assert client.stdout in readable, (
                "requirements with standalone stdin did not return validation"
            )
            assert resolver_started.descriptor not in readable, (
                "requirements with standalone stdin started a resolver"
            )
            client._receive(invalid)
            assert invalid["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "requirements-only `send` performs standalone "
                            "preparation and cannot also queue stdin"
                        ),
                    }
                ],
                "isError": True,
            }, invalid
            assert not resolver_counter.exists(), resolver_counter
            assert not worker_started.exists(), worker_started
            assert not list(
                temporary.glob("mcp-console-tmp-*/mcp-console-server-relay-wire.jsonl")
            )

            preparation = client._start_send(
                requirements={"r": ["standalone-requirement"]},
                timeout_ms=0,
            )
            resolver_started.wait("standalone requirement resolver")
            assert not worker_started.exists(), worker_started
            assert not list(
                temporary.glob("mcp-console-tmp-*/mcp-console-server-relay-wire.jsonl")
            )
            readable, _, _ = select.select([client.stdout], [], [], 0.25)
            assert not readable, "timeout_ms applied to standalone preparation"

            resolver_release.release()
            released = True
            client._receive(preparation)
            assert preparation["result"] == {
                "content": [{"type": "text", "text": "[prepared]"}],
                "isError": False,
            }, preparation
            assert resolver_counter.read_text(encoding="utf-8") == "1"
            assert not worker_started.exists(), worker_started
            assert not list(
                temporary.glob("mcp-console-tmp-*/mcp-console-server-relay-wire.jsonl")
            )

            repeated = client.send(
                requirements={"r": ["standalone-requirement"]},
                timeout_ms=0,
            )
            assert repeated == {
                "content": [{"type": "text", "text": "[prepared]"}],
                "isError": False,
            }, repeated
            assert resolver_counter.read_text(encoding="utf-8") == "1"
            assert not worker_started.exists(), worker_started
            assert not list(
                temporary.glob("mcp-console-tmp-*/mcp-console-server-relay-wire.jsonl")
            )
            transcript = client._finish()
            finished = True
            return transcript
        finally:
            if not released:
                resolver_release.release()
            resolver_started.close()
            resolver_release.close()
            if not finished:
                stop_client(client)


def test_custom_worker_starts_without_home(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment = os.environ.copy()
    environment.pop("HOME", None)
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
        environment,
    )
    client._initialize_and_list_tools()

    client.send(sql="echo echo")
    assert last_tool_text(client) == "zod sql: echo\n"

    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"
    return client._finish()


def test_custom_worker_prepares_r_and_duckdb_requirements(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        isolated_library = temporary_path / "isolated-library"
        isolated_library.mkdir()
        environment["R_LIBS"] = str(isolated_library)
        environment["R_LIBS_SITE"] = str(isolated_library)
        environment["R_LIBS_USER"] = str(isolated_library)
        environment["TMPDIR"] = temporary
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()
        client.send(r="echo echo")

        client.send(requirements={"r": ["praise"]})
        assert last_tool_text(client) == "[prepared]"

        client.send(requirements={"duckdb": ["json"]})
        assert last_tool_text(client) == "[prepared]"

        client.send(r="report managed R requirement")
        assert last_tool_text(client) == "zod R requirement: prepared=true\n"

        client.send(r="fail next r preparation after output")
        assert last_tool_text(client) == "[done]"
        result = client.send(
            r="echo failed preparation cell ran",
            requirements={"r": ["zeallot"]},
        )
        assert result["isError"] is True, result
        assert result["content"] == [
            {"type": "text", "text": "before failed preparation\n"},
            {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
            {
                "type": "text",
                "text": (
                    "\nzod rejected R preparation; further requirement changes "
                    "are unavailable until session restart"
                ),
            },
        ], result

        assert client.temporary_directory is not None
        workspace = Path(client.temporary_directory.name)
        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        events = [
            json.loads(line)
            for line in (session / "internal" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        artifact = events[-2]
        recorded_result = events[-1]
        assert artifact["event"] == "artifact_created", artifact
        assert recorded_result["event"] == "tool_result", recorded_result
        assert artifact["call_id"] == recorded_result["call_id"], events[-2:]
        assert recorded_result["result"]["content"][1] == {
            "type": "image",
            "artifactId": artifact["artifact_id"],
            "path": artifact["path"],
            "mimeType": "image/png",
        }, recorded_result
        assert (session / artifact["path"]).read_bytes() == base64.b64decode(PNG_1X1)

        client.send(r="emit output and image before completion", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        image_started = wait_for_marker(
            temporary_path,
            "zod-image-evaluation-started",
            client,
        )
        (image_started.parent / "zod-release-image").touch()
        wait_for_marker(temporary_path, "zod-image-processed", client)
        try:
            result = client.send(
                r="echo active restart-required cell ran",
                requirements={"r": ["cli"]},
            )
            assert result == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "worker is already evaluating a cell; poll it before "
                            "preparing requirements"
                        ),
                    }
                ],
                "isError": True,
            }, result

            client.send(timeout_ms=0)
            assert client.transcript[-1]["result"] == {
                "content": [
                    {"type": "text", "text": "before pending image\n"},
                    {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                    {
                        "type": "text",
                        "text": "after pending image\n\n[running; poll with an empty send]",
                    },
                ],
                "isError": False,
            }, client.transcript[-1]
        finally:
            (image_started.parent / "zod-release-image-completion").touch()
        client.send(timeout_ms=3_000)
        assert last_tool_text(client) == "[done]"

        result = client.send(
            r="echo restart-required cell ran",
            requirements={"r": ["cli"]},
        )
        assert result == {
            "content": [
                {
                    "type": "text",
                    "text": "requirements require session restart; cell was not run",
                }
            ],
            "isError": True,
        }, result
        client.send(r="echo worker remains usable")
        assert last_tool_text(client) == "zod: worker remains usable\n"

        result = client.send(r="report managed python activation")
        assert result["isError"] is True, result
        failure = result["content"][0]["text"]
        assert "custom worker reported a managed Python activation" in failure, failure
        return client._finish()


def test_custom_worker_reports_idle_input_before_preparation_failure(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        isolated_library = temporary_path / "isolated-library"
        isolated_library.mkdir()
        environment["R_LIBS"] = str(isolated_library)
        environment["R_LIBS_SITE"] = str(isolated_library)
        environment["R_LIBS_USER"] = str(isolated_library)
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()
        client.send(r="request input while idle")
        output = last_tool_text(client)
        assert output == '[input requested: "idle> "]\n', repr(output)

        result = client.send(requirements={"r": ["praise"]})
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            '[idle R callback requested input "idle> " during requirement '
            "preparation; collect callback input with send before preparing requirements]\n"
            "[worker terminated by signal 9]\n"
            "[worker stopped: in-memory state lost]"
        ), result
        result = client.send(requirements={"r": ["zeallot"]})
        assert result == {
            "content": [{"type": "text", "text": "[restart required]"}],
            "isError": False,
        }, result
        return client._finish()


def test_custom_worker_resolves_idle_activity_before_preparation(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        isolated_library = temporary_path / "isolated-library"
        isolated_library.mkdir()
        environment["R_LIBS"] = str(isolated_library)
        environment["R_LIBS_SITE"] = str(isolated_library)
        environment["R_LIBS_USER"] = str(isolated_library)
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()
        client.send(r="resolve python while idle")
        assert last_tool_text(client) == "[done]"

        client.send(requirements={"r": ["praise"]})
        assert last_tool_text(client) == "[prepared]"
        client.send(r="report managed R requirement")
        assert last_tool_text(client) == "zod R requirement: prepared=true\n"
        return client._finish()


def test_combined_requirements_keep_idle_output_as_one_prelude(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        failure = temporary_path / "fail-r-resolution"
        environment["TMPDIR"] = temporary
        environment["MCP_CONSOLE_TEST_R_RESOLUTION_FAILURE"] = str(failure)
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()
        expose_idle_sideband_output(client, temporary_path, "combined-requirements")

        client.send(
            r="echo combined cell",
            requirements={"r": ["praise"]},
        )
        assert last_tool_text(client) == (
            "zod background sideband\n"
            "[output produced while idle]\n"
            "zod: combined cell\n"
        )
        client.send()
        assert last_tool_text(client) == "\n[idle]"

        expose_idle_sideband_output(
            client,
            temporary_path,
            "combined-requirements-failure",
        )
        failure.touch()
        result = client.send(
            r="echo failed resolver cell ran",
            requirements={"r": ["cli"]},
        )
        assert result == {
            "content": [
                {"type": "text", "text": "idle before failure image\n"},
                {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                {
                    "type": "text",
                    "text": (
                        "idle after failure image\n"
                        "[output produced while idle]\n"
                        "R package resolution failed with exit status: 1: "
                        "fixture R resolver failed"
                    ),
                },
            ],
            "isError": True,
        }, result
        failure.unlink()
        client.send(r="echo worker still usable")
        assert last_tool_text(client) == "zod: worker still usable\n"
        return client._finish()


def test_custom_worker_resolves_idle_activity_before_evaluation(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
        environment,
    )
    client._initialize_and_list_tools()
    client.send(r="resolve python while idle")
    assert last_tool_text(client) == "[done]", repr(last_tool_text(client))

    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"

    client.send(r="request input while idle")
    assert last_tool_text(client) == '[input requested: "idle> "]\n'
    client.send(r="echo echo", stdin="continue\n")
    assert last_tool_text(client) == "zod: echo\n"
    return client._finish()


def test_custom_worker_restart_prepares_r_and_duckdb_requirements(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        isolated_library = temporary_path / "isolated-library"
        isolated_library.mkdir()
        environment["R_LIBS"] = str(isolated_library)
        environment["R_LIBS_SITE"] = str(isolated_library)
        environment["R_LIBS_USER"] = str(isolated_library)
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()

        client.send(
            control="restart",
            requirements={"r": ["praise"], "duckdb": ["json"]},
        )
        assert last_tool_text(client) == "[starting new worker]\n[idle]"

        client.send(r="report managed R requirement")
        assert last_tool_text(client) == "zod R requirement: prepared=true\n"
        return client._finish()


def test_captures_worker_stdout(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="emit terminal redraws")
    assert last_tool_text(client) == "ordinary stdout\r\nol\nnew\nold\x1b[2Knew\n"
    return client._finish()


def test_compacts_stdout_and_stderr_independently(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="stress redraws")
    assert last_tool_text(client) == "stress final\nuseful output\n"
    return client._finish()


def test_preserves_invalid_raw_output_when_worker_exits(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment = os.environ.copy()
    with tempfile.TemporaryDirectory() as temporary_directory:
        release = Path(temporary_directory) / "response-gate-released"
        environment["ZOD_TEST_RESPONSE_GATE_RELEASED"] = str(release)
        with ZodFixtureControl() as control:
            control.configure(environment)
            client = SocketGateMcpClient(
                binary,
                ("serve", "--worker", str(zod)),
                environment,
                Path(temporary_directory),
                control.pass_fds,
            )
            control.close_child_ends()
            finished = False
            try:
                client._initialize_and_list_tools()

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
                client.close_test_stdio()


def test_interrupts_running_worker_with_sigint(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment = os.environ.copy()
    with ZodFixtureControl() as control:
        control.configure(environment)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
            pass_fds=control.pass_fds,
        )
        control.close_child_ends()
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
    wrapper = Path(__file__).resolve().parents[2] / "fixtures" / "stop_continue_zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(wrapper)),
            environment,
        )
        workers: list[tuple[int, int]] = []
        passed = False
        try:
            client._initialize_and_list_tools()
            evaluation = client._start_send(r="echo echo", timeout_ms=30_000)
            marker, worker_pid, worker_group = wait_for_stopped_worker(
                temporary_path,
                set(),
                workers,
                client,
            )

            interrupt = client._start_send(control="interrupt", timeout_ms=0)
            readable, _, _ = select.select([client.stdout], [], [], 3)
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


def resolver_interrupt_permission_environment(
    temporary_path: Path,
) -> tuple[dict[str, str], Path, Path]:
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
            printf '%s\n' "$$" > "$MCP_CONSOLE_TEST_RESOLVER_STARTED"
            exec /bin/sleep 30
            """),
        encoding="utf-8",
    )
    fake_ir.chmod(0o755)

    path = environment.get("PATH")
    assert path is not None, "PATH is required"
    environment["PATH"] = os.pathsep.join((str(fake_bin), path))
    environment["TMPDIR"] = str(temporary_path)
    denied_interrupt = temporary_path / "resolver-sigint-denied"
    resolver_started = temporary_path / "resolver-started"
    environment["MCP_CONSOLE_TEST_DENIED_SIGINT"] = str(denied_interrupt)
    environment["MCP_CONSOLE_TEST_RESOLVER_STARTED"] = str(resolver_started)
    # The interposer removes its loader variable after reaching the server, so
    # the resolver and Zod do not inherit it.
    environment["DYLD_INSERT_LIBRARIES"] = str(
        build_killpg_denial_interposer(temporary_path)
    )
    return environment, resolver_started, denied_interrupt


def test_reports_resolver_interrupt_permission_error(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment, resolver_started, denied_interrupt = (
            resolver_interrupt_permission_environment(temporary_path)
        )

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
            resolver_group = int(
                wait_for_marker(
                    temporary_path,
                    resolver_started.name,
                    client,
                ).read_text(encoding="utf-8")
            )
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

            denied_group = int(
                wait_for_marker(
                    temporary_path,
                    denied_interrupt.name,
                    client,
                ).read_text(encoding="utf-8")
            )
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


def test_reports_runtime_r_resolver_interrupt_permission_error(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment, resolver_started, denied_interrupt = (
            resolver_interrupt_permission_environment(temporary_path)
        )
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
            resolver_group = int(
                wait_for_marker(
                    temporary_path,
                    resolver_started.name,
                    client,
                ).read_text(encoding="utf-8")
            )
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

            denied_group = int(
                wait_for_marker(
                    temporary_path,
                    denied_interrupt.name,
                    client,
                ).read_text(encoding="utf-8")
            )
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


def test_accepts_idle_stdin(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(stdin="cold\n")
    assert last_tool_text(client) == "\n[idle]"
    client.send(r="input without request")
    assert last_tool_text(client) == "zod stdin: cold\n"

    client.send(stdin="idle\n")
    assert last_tool_text(client) == "\n[idle]"
    client.send(r="input without request")
    assert last_tool_text(client) == "zod stdin: idle\n"
    return client._finish()


def test_idle_stdin_startup_blocks_preparation(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_release = temporary_path / "zod-startup-release"
        startup_control.write_text("block", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["ZOD_STARTUP_RELEASE"] = str(startup_release)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        passed = False
        try:
            client._initialize_and_list_tools()
            idle_stdin = client._start_send(stdin="queued\n")
            wait_for_marker(
                temporary_path,
                "zod-replacement-waiting-ready",
                client,
            )

            preparation = client._start_send(
                requirements={"python": ["py-yaml12"]},
            )
            client._receive(preparation)
            assert preparation["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": "[requirements not prepared: worker is starting]",
                    }
                ],
                "isError": True,
            }, preparation

            startup_release.touch()
            client._receive(idle_stdin)
            assert idle_stdin["result"] == {
                "content": [{"type": "text", "text": "\n[idle]"}],
                "isError": False,
            }, idle_stdin

            client.send(r="input without request")
            assert last_tool_text(client) == "zod stdin: queued\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            startup_release.touch()
            if not passed:
                stop_process(client.process)


def test_routes_combined_and_followup_stdin(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(
        r="input length without request",
        stdin=("x" * 1024) + "café\0\n",
    )
    client.transcript[-1]["send"]["stdin"] = "<long UTF-8 stdin containing NUL>"
    assert last_tool_text(client) == "zod stdin length: 1030\n"

    client.send(r="input without request", timeout_ms=0)
    assert last_tool_text(client) == "\n[running; poll with an empty send]"
    client.send(stdin="followup\n", timeout_ms=3_000)
    assert last_tool_text(client) == "zod stdin: followup\n"

    client.send(r="request input")
    assert last_tool_text(client) == '[input requested: "zod> "]\n[waiting for stdin]'
    client.send(stdin="")
    assert last_tool_text(client) == "\n[waiting for stdin]"
    client.send(stdin="prompted\n")
    assert last_tool_text(client) == "zod stdin: prompted\n"

    client.send(
        r="input without request then request input",
        stdin="first\n",
        timeout_ms=1_000,
    )
    assert (
        last_tool_text(client) == '[input requested: "second> "]\n[waiting for stdin]'
    )
    client.send(stdin="second\n")
    assert last_tool_text(client) == "zod stdin: first|second\n"

    client.send(r="echo echo", stdin="stale\n")
    assert last_tool_text(client) == "zod: echo\n"
    client.send(r="input without request")
    assert last_tool_text(client) == "zod stdin: stale\n"

    client.send(r="echo echo", stdin="x" * (128 * 1024), timeout_ms=1_000)
    client.transcript[-1]["send"]["stdin"] = "<large unread stdin>"
    assert last_tool_text(client) == "zod: echo\n"
    return client._finish()


def test_routes_same_call_stdin_to_direct_fd0(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="read fd 0 directly", stdin="direct café\n")
    assert last_tool_text(client) == "zod fd 0: 'direct café\\n'\n"
    return client._finish()


def test_preserves_unexposed_input_output(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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

        client.send(
            r="request input after timeout",
            stdin="answer\n",
            timeout_ms=0,
        )
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        waiting = wait_for_marker(
            temporary_path,
            "zod-waiting-to-request-input",
            client,
        )
        (waiting.parent / "zod-release-input-request").touch()
        wait_for_marker(temporary_path, "zod-input-received", client)

        client.send(timeout_ms=3_000)
        assert last_tool_text(client) == (
            'before\n[input requested: "late> "]\nduring request\nzod stdin: answer\n'
        )
        return client._finish()


def last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    return result["content"][0]["text"]


def test_bounds_pending_output_and_resets_after_completion(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="overflow console output")
    output = last_tool_text(client)
    retained = "x" * PENDING_TEXT_BUDGET
    notice = (
        "\n[output truncated: omitted 7 text bytes and "
        "0 encoded image bytes across 1 event]"
    )
    assert output == retained + notice, (
        f"unexpected bounded output: length={len(output)}, tail={output[-200:]!r}"
    )
    client.transcript[-1]["result"]["content"][0]["text"] = (
        f"<retained {PENDING_TEXT_BUDGET} text bytes>{notice}"
    )

    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"
    return client._finish()


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


def test_orders_failure_and_replacement_output(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        startup_control = Path(temporary_directory) / "zod-startup-control"
        startup_control.write_text("ready", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()

        client.send(r="complete silently")
        assert last_tool_text(client) == "[done]"
        startup_control.write_text("ready", encoding="utf-8")
        client.send(r="violate protocol after stdout")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert len(result["content"]) == 1, result
        output = result["content"][0]["text"]
        raw = large_output("zod old stdout\n")
        notices = [
            "[worker sent an unexpected ready message]",
            "[worker terminated by signal 9]",
            "[worker stopped: in-memory state lost]",
            "[starting new worker]",
            "[idle]",
        ]
        assert output.count(raw) == 1, "protocol failure lost raw stdout bytes"
        assert all(output.count(notice) == 1 for notice in notices), repr(output)
        assert [output.index(notice) for notice in notices] == sorted(
            output.index(notice) for notice in notices
        ), repr(output)
        remainder = output.replace(raw, "")
        for notice in notices:
            remainder = remainder.replace(notice, "")
        assert not remainder.replace("\n", ""), repr(output)
        result["content"][0]["text"] = (
            "zod old stdout\n<large output>\n"
            "<cross-source position follows serialized observation>\n"
            "[worker sent an unexpected ready message]\n"
            "[worker terminated by signal 9]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )

        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"
        return client._finish()


def test_preserves_raw_output_during_forced_stop(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    for stream in ("stdout", "stderr"):
        client.send(r=f"force stop after raw {stream}")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert len(result["content"]) == 1, result
        output = result["content"][0]["text"]
        raw = f"zod retiring {stream}: �"
        notices = [
            "[worker sent an unexpected ready message]",
            "[worker terminated by signal 9]",
            "[worker stopped: in-memory state lost]",
            "[starting new worker]",
            "[idle]",
        ]
        assert output.count(raw) == 1, repr(output)
        assert all(output.count(notice) == 1 for notice in notices), repr(output)
        assert [output.index(notice) for notice in notices] == sorted(
            output.index(notice) for notice in notices
        ), repr(output)
        remainder = output.replace(raw, "")
        for notice in notices:
            remainder = remainder.replace(notice, "")
        assert not remainder.replace("\n", ""), repr(output)
        result["content"][0]["text"] = (
            f"{raw}\n<cross-source position follows serialized observation>\n"
            + "\n".join(notices)
        )

    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"
    return client._finish()


def test_reports_missing_worker_launch_failure(binary: Path) -> Transcript:
    client = McpClient(
        binary,
        ("serve", "--worker", "/definitely/missing/mcp-console-worker"),
    )
    client._initialize_and_list_tools()

    client.send(r="complete silently")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    failure = result["content"][0]["text"]
    assert failure.startswith("[failed to launch worker: "), failure
    assert failure.endswith("]"), failure
    result["content"][0]["text"] = "[failed to launch worker: <missing executable>]"

    transcript, standard_error = client._finish_with_standard_error()
    if standard_error:
        assert standard_error.strip() == failure.removeprefix("[").removesuffix("]")
    return transcript


def test_reports_replacement_startup_failure_and_retry(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        startup_control = Path(temporary_directory) / "zod-startup-control"
        startup_control.write_text("ready", encoding="utf-8")
        environment, _ = r_test_environment()
        environment["RETICULATE_PYTHON"] = ""
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        record_resolved_r_library(environment, Path(temporary_directory))
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()

        client.send(r="complete silently")
        assert last_tool_text(client) == "[done]"
        startup_control.write_text("fail with stderr", encoding="utf-8")
        failed = client._start_send(r="exit unexpectedly")
        response_returned = threading.Event()
        forced_stop = threading.Event()

        def stop_if_replacement_loops() -> None:
            if not response_returned.wait(5):
                forced_stop.set()
                stop_process(client.process)

        watchdog = threading.Thread(target=stop_if_replacement_loops, daemon=True)
        watchdog.start()
        try:
            client._receive(failed)
        finally:
            response_returned.set()
            watchdog.join()
        assert not forced_stop.is_set(), "replacement startup retried automatically"
        result = failed["result"]
        assert result == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "[worker sideband read failed: worker sideband closed]\n"
                        "[worker exited with status 86]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "zod replacement startup failed\n"
                        "[worker sideband read failed: worker sideband closed]\n"
                        "[worker exited with status 86]"
                    ),
                }
            ],
            "isError": True,
        }, result

        startup_control.write_text("ready", encoding="utf-8")
        client.send(
            r="report managed R requirement",
            requirements={"r": ["praise"]},
        )
        assert last_tool_text(client) == (
            "[starting new worker]\nzod R requirement: prepared=true\n"
        )
        return client._finish()


def test_polls_replacement_startup_after_send_timeout(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_release = temporary_path / "zod-startup-release"
        startup_control.write_text("ready", encoding="utf-8")
        environment, _ = r_test_environment()
        environment["RETICULATE_PYTHON"] = ""
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["ZOD_STARTUP_RELEASE"] = str(startup_release)
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        forced_release = threading.Event()
        response_returned = threading.Event()
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="complete silently")
            assert last_tool_text(client) == "[done]"
            startup_control.write_text(
                "block",
                encoding="utf-8",
            )

            failed = client._start_send(r="exit unexpectedly", timeout_ms=1_000)
            wait_for_marker(
                temporary_path,
                "zod-replacement-waiting-ready",
                client,
            )

            def release_if_send_ignores_timeout() -> None:
                if not response_returned.wait(5):
                    forced_release.set()
                    startup_release.touch()

            watchdog = threading.Thread(
                target=release_if_send_ignores_timeout,
                daemon=True,
            )
            watchdog.start()
            try:
                client._receive(failed)
            finally:
                response_returned.set()
                watchdog.join()
            assert not forced_release.is_set(), "send did not honor its startup timeout"
            assert failed["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[worker sideband read failed: worker sideband closed]\n"
                            "[worker exited with status 86]\n"
                            "[worker stopped: in-memory state lost]\n"
                            "[starting new worker]\n"
                            "[worker starting]"
                        ),
                    }
                ],
                "isError": True,
            }, failed

            client.send(requirements={"python": ["py-yaml12"]})
            assert client.transcript[-1]["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": "[requirements not prepared: worker is starting]",
                    }
                ],
                "isError": True,
            }, client.transcript[-1]

            combined = client.send(
                r="echo startup overlap cell ran",
                requirements={"r": ["praise"]},
            )
            assert combined == {
                "content": [
                    {
                        "type": "text",
                        "text": "[requirements not prepared: worker is starting]",
                    }
                ],
                "isError": True,
            }, combined
            assert not (temporary_path / "resolved-r-library").exists()

            startup_release.touch()
            client.send(timeout_ms=3_000)
            assert last_tool_text(client) == "[idle]"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            startup_release.touch()
            if not passed:
                stop_process(client.process)


def test_orders_explicit_restart_output(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_control.write_text("ready", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()

        client.send(r="wait for stdin close", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        wait_for_marker(
            temporary_path,
            "zod-waiting-for-stdin-close",
            client,
        )

        startup_control.write_text("ready", encoding="utf-8")
        client.send(control="restart")
        result = client.transcript[-1]["result"]
        assert result["isError"] is False, result
        expected = large_output("zod stdin closed\n") + (
            "\n[active evaluation stopped by session restart request]"
            "\n[worker stopped: in-memory state lost]"
            "\n[starting new worker]"
            "\n[idle]"
        )
        assert result["content"] == [{"type": "text", "text": expected}], result
        result["content"][0]["text"] = (
            "zod stdin closed\n<large output>\n"
            "[active evaluation stopped by session restart request]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )

        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"
        return client._finish()


def test_controlled_restart_runs_cell_once_in_fresh_worker(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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

        client.send(r="set controlled restart state")
        assert last_tool_text(client) == "zod controlled state: old\n"
        old_worker = wait_for_marker(
            temporary_path,
            "zod-controlled-restart-old-worker",
            client,
        )
        old_pid = int(old_worker.read_text(encoding="utf-8"))

        client.send(
            control="restart",
            r="inspect controlled restart state",
        )
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "zod controlled state: fresh; evaluation=1\n"
            "[done]"
        )

        evaluations = wait_for_marker(
            temporary_path,
            "zod-controlled-restart-cell-evaluations",
            client,
        )
        records = evaluations.read_text(encoding="utf-8").splitlines()
        assert len(records) == 1, records
        new_pid, state, count = records[0].split()
        assert int(new_pid) != old_pid, records
        assert (state, count) == ("fresh", "1"), records
        assert not process_exists(old_pid), old_pid

        client.send()
        assert last_tool_text(client) == "\n[idle]"
        assert evaluations.read_text(encoding="utf-8").splitlines() == records
        return client._finish()


def test_controlled_interrupt_preserves_idle_worker_startup_failure(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    ordered_ir = (
        Path(__file__).resolve().parents[2] / "fixtures" / "ordered_retirement_ir"
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_control.write_text("fail with stderr", encoding="utf-8")
        library = temporary_path / "resolved-library"
        library.mkdir()
        fake_bin = temporary_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "ir").symlink_to(ordered_ir)
        resolver_started = FifoCheckpoint(temporary_path / "resolver-started")
        resolver_release = FifoCheckpoint(temporary_path / "resolver-release")
        resolver_interrupted = FifoCheckpoint(temporary_path / "resolver-interrupted")

        environment, _ = r_test_environment()
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["MCP_CONSOLE_TEST_IR_COUNTER"] = str(temporary_path / "ir-counter")
        environment["MCP_CONSOLE_TEST_IR_LIBRARIES"] = str(library)
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        environment["MCP_CONSOLE_TEST_IR_INTERRUPTED"] = str(resolver_interrupted.path)

        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        finished = False
        try:
            client._initialize_and_list_tools()
            preparation = client._start_send(
                requirements={"r": ["blocked-resolver"]},
            )
            resolver_started.wait("controlled interrupt R resolver")

            controlled = client._start_send(
                control="interrupt",
                stdin="unused input\n",
            )
            resolver_interrupted.wait("controlled interrupt signal delivery")
            client._receive_many([preparation, controlled])

            assert preparation["result"].get("isError") is True, preparation
            result = controlled["result"]
            assert result == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "zod replacement startup failed\n"
                            "[worker sideband read failed: worker sideband closed]\n"
                            "[worker exited with status 86]"
                        ),
                    }
                ],
                "isError": True,
            }, result

            startup_control.write_text("ready", encoding="utf-8")
            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            finished = True
            return transcript
        finally:
            resolver_release.release()
            resolver_started.close()
            resolver_release.close()
            resolver_interrupted.close()
            if not finished:
                stop_client(client)


def test_control_only_interrupt_returns_while_explicit_preparation_settles(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    ordered_ir = (
        Path(__file__).resolve().parents[2] / "fixtures" / "ordered_retirement_ir"
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        library = temporary_path / "resolved-library"
        library.mkdir()
        fake_bin = temporary_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "ir").symlink_to(ordered_ir)
        resolver_started = FifoCheckpoint(temporary_path / "resolver-started")
        resolver_release = FifoCheckpoint(temporary_path / "resolver-release")
        resolver_interrupted = FifoCheckpoint(temporary_path / "resolver-interrupted")
        interrupt_release = FifoCheckpoint(temporary_path / "interrupt-release")

        environment, _ = r_test_environment()
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_IR_COUNTER"] = str(temporary_path / "ir-counter")
        environment["MCP_CONSOLE_TEST_IR_LIBRARIES"] = str(library)
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        environment["MCP_CONSOLE_TEST_IR_INTERRUPTED"] = str(resolver_interrupted.path)
        environment["MCP_CONSOLE_TEST_IR_INTERRUPT_RELEASE"] = str(
            interrupt_release.path
        )

        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        finished = False
        interrupt_waiting = False
        try:
            client._initialize_and_list_tools()

            def interrupt_preparation(
                preparation_arguments: dict[str, object],
                description: str,
            ) -> None:
                nonlocal interrupt_waiting
                preparation = client._start_send(**preparation_arguments)
                resolver_started.wait(description)

                interrupt = client._start_send(
                    control="interrupt",
                    timeout_ms=0,
                )
                resolver_interrupted.wait(f"{description} interrupt")
                interrupt_waiting = True
                readable, _, _ = select.select([client.stdout], [], [], 3)
                assert client.stdout in readable, (
                    "control-only interrupt waited for explicit preparation to settle"
                )
                client._receive(interrupt)
                assert interrupt["result"] == {
                    "content": [
                        {
                            "type": "text",
                            "text": "\n[running; poll with an empty send]",
                        }
                    ],
                    "isError": False,
                }, interrupt

                interrupt_release.release()
                interrupt_waiting = False
                client._receive(preparation)
                assert preparation["result"] == {
                    "content": [
                        {
                            "type": "text",
                            "text": "R package resolution failed with exit status: 130: ",
                        }
                    ],
                    "isError": True,
                }, preparation

            interrupt_preparation(
                {"requirements": {"r": ["blocked-standalone-resolver"]}},
                "standalone preparation resolver",
            )

            client.send(r="echo worker ready")
            assert last_tool_text(client) == "zod: worker ready\n"

            interrupt_preparation(
                {
                    "r": "echo interrupted preparation cell ran",
                    "requirements": {"r": ["blocked-cell-resolver"]},
                },
                "cell preparation resolver",
            )

            client.send(r="echo worker remains usable")
            assert last_tool_text(client) == "zod: worker remains usable\n"
            transcript = client._finish()
            finished = True
            return transcript
        finally:
            if interrupt_waiting:
                interrupt_release.release()
            resolver_release.release()
            resolver_started.close()
            resolver_release.close()
            resolver_interrupted.close()
            interrupt_release.close()
            if not finished:
                stop_client(client)


def test_restart_preserves_pending_sideband_output(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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

        client.send(control="restart")
        result = client.transcript[-1]["result"]
        assert result == {
            "content": [
                {"type": "text", "text": "before pending image\n"},
                {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                {
                    "type": "text",
                    "text": (
                        "after pending image\n"
                        "[active evaluation stopped by session restart request]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                },
            ],
            "isError": False,
        }, result
        return client._finish()


def test_restart_preserves_completion_boundary_before_idle_output(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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

        client.send(r="start background sideband", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        started = wait_for_marker(
            temporary_path,
            "zod-background-sideband-started",
            client,
        )
        (started.parent / "zod-release-background-sideband").touch()
        wait_for_marker(
            temporary_path,
            "zod-background-sideband-emitted",
            client,
        )

        client.send(control="restart")
        assert last_tool_text(client) == (
            "[done]\n"
            "zod background sideband\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )
        return client._finish()


def test_restart_interrupts_waiting_send(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
        expose_idle_sideband_output(client, temporary_path)

        waiting = client._start_send(
            r="emit output and image before completion",
            timeout_ms=30_000,
        )
        image_started = wait_for_marker(
            temporary_path,
            "zod-image-evaluation-started",
            client,
        )
        (image_started.parent / "zod-release-image").touch()
        wait_for_marker(temporary_path, "zod-image-processed", client)

        restarted = client._start_send(control="restart")
        responses_returned = threading.Event()
        forced_stop = threading.Event()

        def stop_if_calls_block() -> None:
            if not responses_returned.wait(5):
                forced_stop.set()
                stop_process(client.process)

        watchdog = threading.Thread(target=stop_if_calls_block, daemon=True)
        watchdog.start()
        try:
            client._receive(waiting)
            client._receive(restarted)
        finally:
            responses_returned.set()
            watchdog.join()
        assert not forced_stop.is_set(), "restart did not release the waiting send"

        assert restarted["result"] == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "[active evaluation stopped by session restart request]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                },
            ],
            "isError": False,
        }, restarted
        assert waiting["result"] == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "zod background sideband\n"
                        "[output produced while idle]\n"
                        "before pending image\n"
                    ),
                },
                {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                {
                    "type": "text",
                    "text": (
                        "after pending image\n"
                        "[stopped by session restart request before evaluation finished]\n"
                        "[worker stopped: in-memory state lost]"
                    ),
                },
            ],
            "isError": True,
        }, waiting
        return client._finish()


def test_restarts_after_unexpected_sideband_message(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        killpg_marker = temporary_path / "killpg-denied"
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_KILLPG_MARKER"] = str(killpg_marker)
        # The interposer removes its loader variable after reaching the server,
        # so sandbox-exec and Zod do not inherit it.
        environment["DYLD_INSERT_LIBRARIES"] = str(
            build_killpg_denial_interposer(temporary_path)
        )
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="report process group")
            process_group_output = last_tool_text(client)
            process_group_prefix = "zod process group: "
            assert process_group_output.startswith(process_group_prefix), (
                process_group_output
            )
            worker_group = int(
                process_group_output.removeprefix(process_group_prefix).removesuffix(
                    "\n"
                )
            )
            assert process_group_output == f"{process_group_prefix}{worker_group}\n"
            assert worker_group != os.getpgrp(), (
                "Zod did not enter a dedicated process group"
            )
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "zod process group: <process group>\n"
            )
            failed_call = client._start_send(r="violate protocol")
            client._receive(failed_call)
            assert killpg_marker.is_file(), "killpg denial interposer did not run"
            assert int(killpg_marker.read_text(encoding="utf-8")) == worker_group, (
                "killpg denial targeted a different process group"
            )
            result = failed_call["result"]
            assert result["isError"] is True
            actual = result["content"][0]["text"]
            assert actual == (
                "zod output before protocol failure\n"
                "[worker sent an unexpected ready message]\n"
                "[worker terminated by signal 9]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            ), repr(actual)
            restarted_call = client._start_send(r="complete silently")
            client._receive(restarted_call)
            assert last_tool_text(client) == "[done]"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_restarts_after_worker_exit(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(r="exit unexpectedly")
    assert client.transcript[-1]["result"] == {
        "content": [
            {
                "type": "text",
                "text": (
                    "[worker sideband read failed: worker sideband closed]\n"
                    "[worker exited with status 86]\n"
                    "[worker stopped: in-memory state lost]\n"
                    "[starting new worker]\n"
                    "[idle]"
                ),
            }
        ],
        "isError": True,
    }
    client.send(stdin="replacement\n")
    assert last_tool_text(client) == "\n[idle]"
    client.send(r="input without request")
    assert last_tool_text(client) == "zod stdin: replacement\n"
    return client._finish()


def test_reports_unexpected_worker_exit_zero(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="exit zero")
    assert client.transcript[-1]["result"] == {
        "content": [
            {
                "type": "text",
                "text": (
                    "[worker sideband read failed: worker sideband closed]\n"
                    "[worker exited with status 0]\n"
                    "[worker stopped: in-memory state lost]\n"
                    "[starting new worker]\n"
                    "[idle]"
                ),
            }
        ],
        "isError": True,
    }

    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"
    return client._finish()


def test_replaces_worker_after_relay_exit(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        killpg_count = temporary_path / "relay-killpg-count"
        environment = os.environ.copy()
        environment["MCP_CONSOLE_TEST_KILLPG_COUNT_MARKER"] = str(killpg_count)
        environment["DYLD_INSERT_LIBRARIES"] = str(
            build_killpg_denial_interposer(temporary_path)
        )
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_pid = None
        relay_pid = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="kill relay and remain live", timeout_ms=5_000)

            result = client.transcript[-1]["result"]
            assert result["isError"] is True, result
            topology, failure = result["content"][0]["text"].split("\n", 1)
            worker, launcher, relay = topology.split("; ")
            worker_pid = int(worker.removeprefix("zod worker pid: "))
            launcher_pid = int(launcher.removeprefix("launcher pid: "))
            relay_pid = int(relay.removeprefix("relay process group: "))
            assert len({worker_pid, launcher_pid, relay_pid}) == 3, topology
            assert failure == (
                "[worker relay stdout closed before retirement completed]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            ), failure
            result["content"][0]["text"] = (
                "zod worker pid: <worker pid>; "
                "launcher pid: <launcher pid>; "
                "relay process group: <relay process group>\n" + failure
            )
            assert not process_exists(worker_pid), "worker outlived its relay"
            assert not process_exists(relay_pid), "server did not reap the relay"
            assert not process_group_exists(relay_pid), (
                "relay process group outlived the relay"
            )
            count, process_group = map(
                int,
                killpg_count.read_text(encoding="utf-8").split(),
            )
            assert count == 1, "server tried to stop the retired relay group twice"
            assert process_group == relay_pid, (
                "server stopped a different process group"
            )

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(relay_pid)
                stop_process_id(worker_pid)
                stop_process(client.process)


def test_restart_closes_worker_stdin(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
        client.send(r="wait for stdin close", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        wait_for_marker(
            temporary_path,
            "zod-waiting-for-stdin-close",
            client,
        )

        client.send(control="restart")
        output = last_tool_text(client)
        prefix = "zod stdin closed\n" + ("x" * LARGE_OUTPUT_SIZE)
        suffix = "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        suffix = "[active evaluation stopped by session restart request]\n" + suffix
        assert output.startswith(prefix), "worker stdin did not close before restart"
        assert output.endswith(suffix), "lifecycle notices followed old-worker output"
        barrier = output.removeprefix(prefix).removesuffix(suffix)
        assert barrier and not barrier.strip("y\n"), "unexpected old-worker output"
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "zod stdin closed\n<large output>\n"
            "[active evaluation stopped by session restart request]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )

        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"
        return client._finish()


def test_restart_force_stops_stalled_worker(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
            client._initialize_and_list_tools()
            client.send(r="stall", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            group_marker = wait_for_marker(
                temporary_path,
                "zod-process-group",
                client,
            )
            worker_group = read_worker_group(group_marker)
            wait_for_marker(temporary_path, "zod-stalled", client)

            restart_call = client._start_send(control="restart")
            wait_for_process_group_exit(worker_group, client)
            client._receive(restart_call)
            assert last_tool_text(client) == (
                "[active evaluation stopped by session restart request]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            )

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_restart_allows_accepted_relay_shutdown_to_finish(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        helper_pid = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="stall accepted relay shutdown", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            helper_marker = wait_for_marker(
                temporary_path,
                "zod-relay-resume-helper",
                client,
            )
            helper_pid = int(helper_marker.read_text(encoding="utf-8"))

            restarted = client._start_send(control="restart")
            wait_for_marker(
                temporary_path,
                "zod-relay-stopped-after-shutdown",
                client,
            )
            client._receive(restarted)
            restart_output = last_tool_text(client)
            assert restart_output == (
                "zod output during relay retirement\n"
                "[active evaluation stopped by session restart request]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            ), restart_output

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            stop_process_id(helper_pid)
            if not passed:
                stop_process(client.process)


def test_restart_outer_force_stops_unresponsive_relay(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        killpg_marker = temporary_path / "killpg-denied"
        late_member_marker = temporary_path / "late-process-group-member"
        late_member_reap_marker = temporary_path / "late-process-group-member-reaped"
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        environment["MCP_CONSOLE_TEST_KILLPG_MARKER"] = str(killpg_marker)
        environment["MCP_CONSOLE_TEST_LATE_MEMBER_MARKER"] = str(late_member_marker)
        environment["MCP_CONSOLE_TEST_LATE_MEMBER_REAP_MARKER"] = str(
            late_member_reap_marker
        )
        environment["DYLD_INSERT_LIBRARIES"] = str(
            build_killpg_denial_interposer(temporary_path)
        )
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        helper_pid = None
        worker_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="stall with stopped relay", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            helper_marker = wait_for_marker(
                temporary_path,
                "zod-relay-stop-helper",
                client,
            )
            helper_pid = int(helper_marker.read_text(encoding="utf-8"))
            wait_for_marker(temporary_path, "zod-relay-stopped", client)
            relay_target, launcher_pid = map(
                int,
                wait_for_marker(
                    temporary_path,
                    "zod-relay-stop-target",
                    client,
                )
                .read_text(encoding="utf-8")
                .split(),
            )
            worker_group = read_worker_group(
                wait_for_marker(temporary_path, "zod-process-group", client)
            )
            assert relay_target == worker_group, (
                "helper did not stop the sandbox process-group leader"
            )
            assert launcher_pid != relay_target, (
                "Zod launcher unexpectedly identified the relay"
            )
            assert os.getpgid(launcher_pid) == relay_target, (
                "Zod launcher did not inherit the relay process group"
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

            restart_started = time.monotonic()
            receiver = threading.Thread(target=receive_restart, daemon=True)
            receiver.start()
            assert received.wait(2), "restart outlived its original shutdown deadline"
            restart_elapsed = time.monotonic() - restart_started
            receiver.join()
            if errors:
                raise errors[0]

            assert restart_elapsed < 2, f"restart took {restart_elapsed:.3f} seconds"
            assert int(killpg_marker.read_text(encoding="utf-8")) == worker_group
            late_member, late_member_group = map(
                int,
                late_member_marker.read_text(encoding="utf-8").split(),
            )
            assert late_member > 0, "invalid late process-group member PID"
            assert late_member_group == worker_group, (
                "late member joined a different process group"
            )
            assert int(late_member_reap_marker.read_text(encoding="utf-8")) == (
                late_member
            ), "a different late process-group member was reaped"
            assert not process_group_exists(worker_group), (
                "stopped relay process group outlived restart"
            )
            assert not process_exists(relay_target), "server did not reap the relay"
            assert last_tool_text(client) == (
                "[active evaluation stopped by session restart request]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            )

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            stop_process_id(helper_pid)
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_restart_starts_first_worker_and_waits_until_ready(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_release = temporary_path / "zod-startup-release"
        startup_control.write_text("block", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["ZOD_STARTUP_RELEASE"] = str(startup_release)
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            restarted = client._start_send(control="restart")
            wait_for_marker(
                temporary_path,
                "zod-replacement-waiting-ready",
                client,
            )
            worker_group = read_worker_group(
                wait_for_marker(temporary_path, "zod-process-group", client)
            )

            while_restarting = client._start_send(r="echo echo")
            client._receive(while_restarting)
            result = while_restarting["result"]
            assert result["isError"] is True
            assert result["content"][0]["text"] == "[worker is restarting]"

            startup_release.touch()
            client._receive(restarted)
            assert restarted["result"]["content"][0]["text"] == (
                "[starting new worker]\n[idle]"
            )

            after_restart = client._start_send(r="echo echo")
            client._receive(after_restart)
            assert after_restart["result"]["content"][0]["text"] == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_restart_does_not_report_never_ready_worker_as_stopped(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_release = temporary_path / "zod-startup-release"
        startup_control.write_text(
            "block with detached sideband writer",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["ZOD_STARTUP_RELEASE"] = str(startup_release)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        descendant_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            waiting = client._start_send(r="echo echo", timeout_ms=30_000)
            wait_for_marker(
                temporary_path,
                "zod-replacement-waiting-ready",
                client,
            )
            marker = wait_for_marker(
                temporary_path,
                "zod-detached-startup-sideband-pid",
                client,
            )
            descendant_group = int(marker.read_text(encoding="utf-8"))

            startup_control.write_text("ready", encoding="utf-8")
            restarted = client._start_send(control="restart")
            responses_returned = threading.Event()
            forced_stop = threading.Event()

            def stop_if_calls_block() -> None:
                if not responses_returned.wait(5):
                    forced_stop.set()
                    stop_process(client.process)

            watchdog = threading.Thread(target=stop_if_calls_block, daemon=True)
            watchdog.start()
            try:
                client._receive(waiting)
                client._receive(restarted)
            finally:
                responses_returned.set()
                watchdog.join()
            assert not forced_stop.is_set(), "restart did not finish initial startup"

            assert waiting["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[stopped by session restart request before "
                            "evaluation finished]"
                        ),
                    }
                ],
                "isError": True,
            }, waiting
            assert restarted["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[active evaluation stopped by session restart request]\n"
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
            startup_release.touch()
            stop_process_group(descendant_group)
            if not passed:
                stop_process(client.process)


def test_restart_commits_lifecycle_before_replacement_callbacks(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_control.write_text("ready", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()
        client.send(r="complete silently")
        assert last_tool_text(client) == "[done]"

        startup_control.write_text("ready with callback", encoding="utf-8")
        client.send(control="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        callback = wait_for_marker(
            temporary_path,
            "zod-startup-callback-response",
            client,
        )
        assert callback.read_text(encoding="utf-8") == (
            "Python requirements are unavailable with a custom worker"
        )
        callback.unlink()

        client.send(control="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        callback = wait_for_marker(
            temporary_path,
            "zod-startup-callback-response",
            client,
        )
        assert callback.read_text(encoding="utf-8") == (
            "Python requirements are unavailable with a custom worker"
        )

        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"
        return client._finish()


def test_restart_discards_unread_stdin(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(stdin="stale\n")
    assert last_tool_text(client) == "\n[idle]"

    client.send(control="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )

    client.send(r="input without request", stdin="fresh\n")
    assert last_tool_text(client) == "zod stdin: fresh\n"
    return client._finish()


def test_retries_initial_startup_silently(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        startup_control = Path(temporary_directory) / "zod-startup-control"
        startup_control.write_text("fail", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()
        client.send(r="echo echo")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "[worker sideband read failed: worker sideband closed]\n"
            "[worker exited with status 86]"
        )
        startup_control.write_text("ready", encoding="utf-8")
        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"
        return client._finish()


def test_runs_worker_inside_sandbox(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
        client._initialize_and_list_tools()
        client.send(r="probe sandbox")
        transcript = client._finish()

        assert host_file.read_text(encoding="utf-8") == "host data"
        return transcript


def test_shuts_down_stalled_worker(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
            client._initialize_and_list_tools()
            stalled = client._start_send(
                r="stall",
                stdin="x" * (2 * 1024 * 1024),
            )
            stalled["send"]["stdin"] = "<large stdin>"
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


def test_shutdown_is_bounded_with_detached_stdin_descendant(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment = os.environ.copy()
    with ZodFixtureControl() as control:
        control.configure(environment)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
            pass_fds=control.pass_fds,
        )
        control.close_child_ends()
        descendant_group = None
        descendant_cleaned = False
        worker_group = None
        server_stopped = False
        operation = None
        try:
            client._initialize_and_list_tools()
            client.send(r="echo ready")
            assert last_tool_text(client) == "zod: ready\n"

            operation = client._next_request_id
            client.send(
                r=f"stall with detached stdin: {operation}",
                timeout_ms=0,
            )
            submitted = client.transcript[-1]
            assert submitted["id"] == operation, submitted
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            submitted["send"]["r"] = "<stall with detached stdin>"

            control.wait_for(operation, "worker_operation_started")
            created = control.wait_for(operation, "detached_descendant_created")
            created_group = created["process_group"]
            assert isinstance(created_group, int) and created_group > 0, created
            assert created["pid"] == created_group, created
            assert created_group != os.getpgrp(), created
            assert created["inherited_fd"] == 0, created
            retained_fd = created["retained_fd"]
            assert isinstance(retained_fd, int) and retained_fd > 2, created
            descendant_group = created_group

            control.wait_for(operation, "parent_waiting_for_stdin")
            probe_start = len(client.transcript)
            expected_bytes = 0
            chunk_bytes = 64 * 1024
            while True:
                assert expected_bytes + chunk_bytes <= PENDING_TEXT_BUDGET, (
                    "worker stdin remained fully buffered through the adaptive probe; "
                    + control.diagnostics()
                )
                request = client._next_request_id
                client.send(stdin="x" * chunk_bytes, timeout_ms=0)
                probe = client.transcript[-1]
                assert probe["id"] == request, probe
                assert last_tool_text(client) == (
                    "\n[running; poll with an empty send]"
                )
                expected_bytes += chunk_bytes
                control.send_control(
                    operation,
                    "probe_stdin",
                    request=request,
                    expected_bytes=expected_bytes,
                )
                observed = control.wait_for_any(
                    request,
                    {"stdin_write_buffered", "stdin_write_pending"},
                )
                assert observed["target_operation"] == operation, observed
                assert observed["expected_bytes"] == expected_bytes, observed
                consumed_bytes = observed["consumed_bytes"]
                queued_bytes = observed["queued_bytes"]
                assert isinstance(consumed_bytes, int), observed
                assert isinstance(queued_bytes, int) and queued_bytes > 0, observed
                assert consumed_bytes + queued_bytes <= expected_bytes, observed
                if observed["kind"] == "stdin_write_pending":
                    assert consumed_bytes + queued_bytes < expected_bytes, observed
                    break
                assert consumed_bytes + queued_bytes == expected_bytes, observed
                chunk_bytes *= 2

            probes = client.transcript[probe_start:]
            adaptive_probe = probes[0]
            adaptive_probe["send"]["stdin"] = "<adaptive stdin probe>"
            adaptive_probe["result"] = probes[-1]["result"]
            client.transcript[probe_start:] = [adaptive_probe]

            stalled_event = control.wait_for(operation, "parent_operation_stalled")
            stalled_group = stalled_event["process_group"]
            assert isinstance(stalled_group, int) and stalled_group > 0, stalled_event
            assert stalled_group != os.getpgrp(), stalled_event
            assert stalled_group != descendant_group, stalled_event
            worker_group = stalled_group

            stalled = client._start_send(timeout_ms=30_000)
            client.send(timeout_ms=0)
            polling = client.transcript[-1]
            assert polling["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": "[worker evaluation is already being polled]",
                    }
                ],
                "isError": True,
            }, polling

            shutdown_started = time.monotonic()
            client.stdin.close()
            try:
                return_code = client.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    "mcp-console did not stop with detached worker stdin; "
                    + control.diagnostics()
                ) from None
            shutdown_elapsed = time.monotonic() - shutdown_started
            server_stopped = True

            client._receive(stalled)
            assert stalled["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": "[worker stopped before operation completed]",
                    }
                ],
                "isError": True,
            }, stalled
            stalled["id"] = "<pending poll request>"
            polling["id"] = "<poll ownership request>"
            standard_error = client.stderr.read()
            assert return_code == 0, standard_error
            assert client.stdout.read() == ""
            assert standard_error == ""
            assert shutdown_elapsed < 2, (
                f"worker shutdown took {shutdown_elapsed:.3f} seconds; "
                + control.diagnostics()
            )
            assert not process_group_exists(worker_group), (
                "worker process group outlived mcp-console shutdown; "
                + control.diagnostics()
            )

            control.release_cleanup()
            cleaned = control.wait_for(operation, "fixture_cleanup_completed")
            assert cleaned["pid"] == descendant_group, cleaned
            control.wait_for_eof()
            descendant_cleaned = True
            return client.transcript
        finally:
            control.release_cleanup()
            if not server_stopped:
                stop_process(client.process)
            try:
                if (
                    operation is not None
                    and descendant_group is not None
                    and not descendant_cleaned
                ):
                    cleaned = control.wait_for(
                        operation,
                        "fixture_cleanup_completed",
                    )
                    assert cleaned["pid"] == descendant_group, cleaned
                    control.wait_for_eof()
                    descendant_cleaned = True
            finally:
                if not descendant_cleaned:
                    stop_process_group(descendant_group)
                stop_process_group(worker_group)


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


def test_demarcates_idle_prelude_across_cell_outcomes(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        passed = False
        try:
            client._initialize_and_list_tools()

            expose_idle_sideband_output(client, temporary_path, "success")
            client.send(r="echo echo")
            assert last_tool_text(client) == (
                "zod background sideband\n[output produced while idle]\nzod: echo\n"
            )

            expose_idle_sideband_output(client, temporary_path, "timeout")
            timed_out = client._start_send(
                r="output then complete after release",
                timeout_ms=1_000,
            )
            processed = wait_for_marker(
                temporary_path,
                "zod-cell-output-processed",
                client,
            )
            client._receive(timed_out)
            assert timed_out["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "zod background sideband\n"
                            "[output produced while idle]\n"
                            "zod cell output before completion\n\n"
                            "[running; poll with an empty send]"
                        ),
                    }
                ],
                "isError": False,
            }, timed_out

            (processed.parent / "zod-release-evaluation").touch()
            client.send(timeout_ms=3_000)
            assert last_tool_text(client) == (
                "zod: output then complete after release\n"
            ), repr(last_tool_text(client))

            expose_idle_sideband_output(client, temporary_path, "input")
            client.send(r="request input", timeout_ms=3_000)
            assert last_tool_text(client) == (
                "zod background sideband\n"
                "[output produced while idle]\n"
                '[input requested: "zod> "]\n'
                "[waiting for stdin]"
            )
            client.send(stdin="answer\n", timeout_ms=3_000)
            assert last_tool_text(client) == "zod stdin: answer\n"

            expose_idle_sideband_output(client, temporary_path, "language-error")
            client.send(r="language error")
            assert last_tool_text(client) == (
                "zod background sideband\n"
                "[output produced while idle]\n"
                "zod language error\n"
            )

            expose_idle_sideband_output(client, temporary_path, "replacement")
            client.send(r="exit unexpectedly")
            result = client.transcript[-1]["result"]
            assert result["isError"] is True, result
            assert result["content"] == [
                {
                    "type": "text",
                    "text": (
                        "zod background sideband\n"
                        "[output produced while idle]\n"
                        "[worker sideband read failed: worker sideband closed]\n"
                        "[worker exited with status 86]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                }
            ], result
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process(client.process)


def test_restart_cancels_partial_sideband_frame(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
            assert received.wait(3), "restart waited for a partial sideband frame"
            receiver.join()
            if errors:
                raise errors[0]
            assert last_tool_text(client) == (
                "[active evaluation stopped by session restart request]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            )

            stop_process_group(descendant_group)
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
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
            assert received.wait(3), "restart waited for the sideband reader"
            receiver.join()
            if errors:
                raise errors[0]
            assert last_tool_text(client) == (
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            )

            stop_process_group(descendant_group)
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


def test_shutdown_cancels_partial_sideband_frame(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
            stop_process_group(descendant_group)
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
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
            client._initialize_and_list_tools()
            entry = client._start_send(r="x" * (2 * 1024 * 1024))
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
            entry["send"]["r"] = "<large cell>"
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


def wait_for_stopped_worker(
    root: Path,
    previous_process_ids: set[int],
    recorded_workers: list[tuple[int, int]],
    client: McpClient,
) -> tuple[Path, int, int]:
    deadline = time.monotonic() + 3
    while True:
        for marker in root.glob("**/zod-stop-continue-worker"):
            process_id, parent_id, process_group = map(
                int,
                marker.read_text(encoding="utf-8").split(),
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
            "direct worker did not enter the stopped process state"
        )
        time.sleep(0.01)


def wait_for_path(path: Path, description: str, client: McpClient) -> None:
    deadline = time.monotonic() + 3
    while not path.exists():
        assert client.process.poll() is None, (
            f"mcp-console stopped before {description}"
        )
        assert time.monotonic() < deadline, f"timed out waiting for {description}"
        time.sleep(0.01)


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
    deadline = time.monotonic() + 3
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
    deadline = time.monotonic() + 3
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


if __name__ == "__main__":
    run_this_suite(__file__)
