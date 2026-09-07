import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Self, TextIO

from support.records import ToolResult, Transcript, TranscriptEntry

SERVER_SHUTDOWN_SECONDS = 11
SERVER_REAP_SECONDS = 2
CASE_RESPONSE_RESERVE_SECONDS = SERVER_SHUTDOWN_SECONDS + SERVER_REAP_SECONDS + 1


class TextReader:
    """Descriptor-backed text with an explicit buffer and bounded reads."""

    def __init__(self, stream: TextIO | socket.socket) -> None:
        self.stream = stream
        self.buffer = bytearray()
        self.eof = False
        self.closed = False

    def fileno(self) -> int:
        return self.stream.fileno()

    def fill(self) -> None:
        chunk = os.read(self.fileno(), 64 * 1024)
        self.buffer.extend(chunk)
        self.eof = not chunk

    def _wait(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([self], [], [], remaining)[0]:
            raise TimeoutError("timed out reading test transport")
        self.fill()

    def readline(self, timeout: float = 15) -> str:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer and not self.eof:
            self._wait(deadline)
        line, separator, remainder = self.buffer.partition(b"\n")
        self.buffer = bytearray(remainder)
        return (line + separator).decode("utf-8")

    def read(self, timeout: float = 15) -> str:
        deadline = time.monotonic() + timeout
        while not self.eof:
            self._wait(deadline)
        result = self.buffer.decode("utf-8")
        self.buffer.clear()
        return result

    def close(self) -> None:
        self.stream.close()
        self.eof = True
        self.closed = True


class McpClient:
    """A synchronous, recording MCP client; use a context to own its lifetime."""

    response_timeout: float = 600
    shutdown_timeout: float = SERVER_SHUTDOWN_SECONDS

    def __init__(
        self,
        binary: Path,
        arguments: tuple[str, ...] = (),
        environment: dict[str, str] | None = None,
        current_directory: Path | None = None,
        umask: int = -1,
        pass_fds: tuple[int, ...] = (),
        response_timeout: float = 600,
        shutdown_timeout: float = SERVER_SHUTDOWN_SECONDS,
    ) -> None:
        self.response_timeout = response_timeout
        self.shutdown_timeout = shutdown_timeout
        self.temporary_directory = (
            tempfile.TemporaryDirectory() if current_directory is None else None
        )
        if current_directory is None:
            assert self.temporary_directory is not None
            current_directory = Path(self.temporary_directory.name)
        if (
            sys.platform == "linux"
            and arguments[:1] == ("serve",)
            and "--no-sandbox" not in arguments
        ):
            arguments = (*arguments, "--no-sandbox")
        process = subprocess.Popen(
            [binary, *arguments],
            env=environment,
            cwd=current_directory,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            umask=umask,
            pass_fds=pass_fds,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        self.process = process
        self.stdin = process.stdin
        self.stdout = TextReader(process.stdout)
        self.stderr = TextReader(process.stderr)
        self.transcript: Transcript = []
        self._next_request_id = 1
        self._issued_request_ids: set[int] = set()
        self._last_serialized_message: str | None = None

    def send(self, **arguments: Any) -> ToolResult:
        return self._call_tool("send", **arguments)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()

    def send_message(self, message: dict[str, Any]) -> TranscriptEntry:
        recorded_message = message.copy()
        assert recorded_message.pop("jsonrpc", None) == "2.0", message

        entry = {}
        if "id" in recorded_message:
            request_id = recorded_message.pop("id")
            assert isinstance(request_id, int), message
            assert request_id not in self._issued_request_ids, (
                f"JSON-RPC request ID was reused: {request_id}"
            )
            self._issued_request_ids.add(request_id)
            entry["id"] = request_id
        params = recorded_message.get("params")
        if (
            recorded_message.keys() == {"method", "params"}
            and recorded_message["method"] == "tools/call"
            and isinstance(params, dict)
            and params.keys() == {"name", "arguments"}
            and params["name"] == "send"
            and isinstance(params["arguments"], dict)
        ):
            entry[params["name"]] = params["arguments"]
        else:
            entry["input"] = recorded_message
        self.transcript.append(entry)
        serialized_message = json.dumps(message, ensure_ascii=False)
        self._last_serialized_message = serialized_message
        self.stdin.write(serialized_message + "\n")
        self.stdin.flush()
        return entry

    def _read_response_line(self) -> str:
        deadline = time.monotonic() + self.response_timeout
        if (
            case_deadline := os.environ.get("MCP_CONSOLE_TEST_CASE_DEADLINE")
        ) is not None:
            deadline = min(
                deadline, float(case_deadline) - CASE_RESPONSE_RESERVE_SECONDS
            )
        while b"\n" not in self.stdout.buffer and not self.stdout.eof:
            self._wait_for_output(deadline, "response")
        if self.stdout.buffer:
            return self.stdout.readline()
        raise AssertionError(
            f"mcp-console stdout closed before replying: {self._diagnostics()}"
        )

    def _diagnostics(self) -> str:
        tail = self.stderr.buffer[-64 * 1024 :].decode("utf-8", errors="replace")
        return f"return_code={self.process.poll()!r}, stderr={tail!r}"

    def _wait_for_output(self, deadline: float, stage: str) -> None:
        streams = [stream for stream in (self.stdout, self.stderr) if not stream.eof]
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not (
            ready := select.select(streams, [], [], remaining)[0]
        ):
            raise TimeoutError(
                f"mcp-console timed out waiting for {stage}: {self._diagnostics()}"
            )
        for stream in ready:
            stream.fill()

    def receive(self, entry: TranscriptEntry) -> None:
        line = self._read_response_line()
        message = json.loads(line)
        assert message.pop("jsonrpc", None) == "2.0", message
        assert message.pop("id", None) == entry["id"], message
        assert message.keys() == {"result"} or message.keys() == {"error"}, message
        assert entry.keys().isdisjoint(message), message
        entry.update(message)

    def receive_many(self, entries: list[TranscriptEntry]) -> None:
        pending = {entry["id"]: entry for entry in entries}
        assert len(pending) == len(entries), "response batch reused a request ID"
        for _ in entries:
            line = self._read_response_line()
            message = json.loads(line)
            assert message.pop("jsonrpc", None) == "2.0", message
            request_id = message.pop("id", None)
            assert request_id in pending, message
            entry = pending.pop(request_id)
            assert message.keys() == {"result"} or message.keys() == {"error"}, message
            assert entry.keys().isdisjoint(message), message
            entry.update(message)

    def start_request(self, method: str, **params: Any) -> TranscriptEntry:
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_request_id,
            "method": method,
        }
        self._next_request_id += 1
        if params:
            message["params"] = params

        return self.send_message(message)

    def request(self, method: str, **params: Any) -> TranscriptEntry:
        entry = self.start_request(method, **params)
        self.receive(entry)
        return entry

    def notify(self, method: str, **params: Any) -> None:
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params:
            message["params"] = params

        self.send_message(message)

    def initialize_and_list_tools(self) -> None:
        self.request(
            "initialize",
            protocolVersion="2025-11-25",
            capabilities={},
            clientInfo={
                "name": "acceptance-test",
                "version": "1.0.0",
            },
        )
        self.notify("notifications/initialized")
        self.request("tools/list")

    def _start_tool_call(self, name: str, **arguments: Any) -> TranscriptEntry:
        return self.start_request(
            "tools/call",
            name=name,
            arguments=arguments,
        )

    def _call_tool(self, name: str, **arguments: Any) -> ToolResult:
        entry = self._start_tool_call(name, **arguments)
        self.receive(entry)
        result = entry["result"]
        assert isinstance(result, dict), result
        return result

    def start_send(self, **arguments: Any) -> TranscriptEntry:
        return self._start_tool_call("send", **arguments)

    def finish(self) -> Transcript:
        transcript, standard_error = self.finish_with_standard_error()
        assert standard_error == "", standard_error
        return transcript

    def finish_with_standard_error(self) -> tuple[Transcript, str]:
        deadline = self._cleanup_deadline()
        try:
            self._shutdown(deadline - SERVER_REAP_SECONDS)
            extra_output = self.stdout.read()
            standard_error = self.stderr.read()
            assert self.process.returncode == 0, standard_error
            assert extra_output == "", f"unexpected extra output: {extra_output}"
            return self.transcript, standard_error
        finally:
            self._dispose(deadline)

    def _cleanup_deadline(self) -> float:
        timeout = self.shutdown_timeout
        if "MCP_CONSOLE_TEST_CASE_DEADLINE" in os.environ:
            timeout = min(timeout, SERVER_SHUTDOWN_SECONDS)
        # Normal server retirement has up to ten seconds of staged deadlines.
        # Reserve PID kill/reap time inside the supervisor's fifteen seconds.
        return time.monotonic() + timeout + SERVER_REAP_SECONDS

    def _shutdown(self, deadline: float) -> None:
        if not self.stdin.closed:
            self.stdin.close()
        while not (self.stdout.eof and self.stderr.eof):
            self._wait_for_output(deadline, "shutdown")
        try:
            self.process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise TimeoutError(
                f"mcp-console timed out waiting for shutdown: {self._diagnostics()}"
            ) from None

    def _dispose(self, deadline: float) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(
            timeout=min(SERVER_REAP_SECONDS, max(0, deadline - time.monotonic()))
        )
        for stream in (self.stdin, self.stdout, self.stderr):
            stream.close()
        if self.temporary_directory is not None:
            self.temporary_directory.cleanup()

    def close(self) -> None:
        """Close input, allow staged retirement, then kill only the server PID."""
        if (
            self.stdout.closed
            and self.stderr.closed
            and self.process.poll() is not None
        ):
            return
        deadline = self._cleanup_deadline()
        try:
            self._shutdown(deadline - SERVER_REAP_SECONDS)
        except TimeoutError:
            pass
        finally:
            self._dispose(deadline)


def stop_client(client: McpClient) -> None:
    client.close()
