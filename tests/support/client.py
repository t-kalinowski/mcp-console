import json
import os
import select
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from support.records import ToolResult, Transcript, TranscriptEntry


class McpClient:
    def __init__(
        self,
        binary: Path,
        arguments: tuple[str, ...] = (),
        environment: dict[str, str] | None = None,
        current_directory: Path | None = None,
        umask: int = -1,
        pass_fds: tuple[int, ...] = (),
    ) -> None:
        self.temporary_directory = (
            tempfile.TemporaryDirectory() if current_directory is None else None
        )
        if current_directory is None:
            assert self.temporary_directory is not None
            current_directory = Path(self.temporary_directory.name)
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
        self.stdout = process.stdout
        self.stderr = process.stderr
        self.transcript: Transcript = []
        self._next_request_id = 1
        self._issued_request_ids: set[int] = set()

    def send(self, **arguments: Any) -> ToolResult:
        return self._call_tool("send", **arguments)

    def _send_message(self, message: dict[str, Any]) -> TranscriptEntry:
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
        self.stdin.write(json.dumps(message) + "\n")
        self.stdin.flush()
        return entry

    def _read_response_line(self) -> str:
        line = self.stdout.readline()
        if line:
            return line

        return_code = self.process.poll()
        standard_error = ""
        readable, _, _ = select.select([self.stderr], [], [], 0)
        if readable:
            standard_error = os.read(self.stderr.fileno(), 64 * 1024).decode(
                "utf-8",
                errors="replace",
            )
        raise AssertionError(
            "mcp-console stdout closed before replying: "
            f"return_code={return_code!r}, stderr={standard_error!r}"
        )

    def _receive(self, entry: TranscriptEntry) -> None:
        line = self._read_response_line()
        message = json.loads(line)
        assert message.pop("jsonrpc", None) == "2.0", message
        assert message.pop("id", None) == entry["id"], message
        assert message.keys() == {"result"} or message.keys() == {"error"}, message
        assert entry.keys().isdisjoint(message), message
        entry.update(message)

    def _receive_many(self, entries: list[TranscriptEntry]) -> None:
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

    def _start_request(self, method: str, **params: Any) -> TranscriptEntry:
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_request_id,
            "method": method,
        }
        self._next_request_id += 1
        if params:
            message["params"] = params

        return self._send_message(message)

    def _request(self, method: str, **params: Any) -> TranscriptEntry:
        entry = self._start_request(method, **params)
        self._receive(entry)
        return entry

    def _notify(self, method: str, **params: Any) -> None:
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params:
            message["params"] = params

        self._send_message(message)

    def _initialize_and_list_tools(self) -> None:
        self._request(
            "initialize",
            protocolVersion="2025-11-25",
            capabilities={},
            clientInfo={
                "name": "acceptance-test",
                "version": "1.0.0",
            },
        )
        self._notify("notifications/initialized")
        self._request("tools/list")

    def _start_tool_call(self, name: str, **arguments: Any) -> TranscriptEntry:
        return self._start_request(
            "tools/call",
            name=name,
            arguments=arguments,
        )

    def _call_tool(self, name: str, **arguments: Any) -> ToolResult:
        entry = self._start_tool_call(name, **arguments)
        self._receive(entry)
        result = entry["result"]
        assert isinstance(result, dict), result
        return result

    def _start_send(self, **arguments: Any) -> TranscriptEntry:
        return self._start_tool_call("send", **arguments)

    def _finish(self) -> Transcript:
        transcript, standard_error = self._finish_with_standard_error()
        assert standard_error == "", standard_error
        return transcript

    def _finish_with_standard_error(self) -> tuple[Transcript, str]:
        self.stdin.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            stdout = executor.submit(self.stdout.read)
            stderr = executor.submit(self.stderr.read)
            return_code = self.process.wait()
            extra_output = stdout.result()
            standard_error = stderr.result()

        assert return_code == 0, standard_error
        assert extra_output == "", f"unexpected extra output: {extra_output}"
        return self.transcript, standard_error


def stop_client(client: McpClient) -> None:
    if client.process.poll() is not None:
        return
    if not client.stdin.closed:
        client.stdin.close()
    try:
        client.process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        client.process.kill()
        client.process.wait()
