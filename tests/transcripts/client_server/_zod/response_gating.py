import array
import fcntl
import os
import select
import socket
import subprocess
import threading
import termios
import time
from pathlib import Path

from _support import McpClient

from .coordination import (
    FIXTURE_CHECKPOINT_TIMEOUT_SECONDS,
    TEST_CONTROL_READY_NAME,
    TEST_RESPONSE_QUERY_FIFO_NAME,
    TEST_RESPONSE_RESULT_FIFO_NAME,
    ZodFixtureControl,
)


TEST_GATED_RESPONSE_SIZE = 128 * 1024


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
