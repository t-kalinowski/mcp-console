import json
import os
import select
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from textwrap import dedent
from typing import Any


TranscriptEntry = dict[str, Any]
Transcript = list[TranscriptEntry]


def code(source: str) -> str:
    return dedent(source).removeprefix("\n")


def run_this_suite(suite_path: str) -> None:
    suite = Path(suite_path).resolve()
    root = suite.parents[2]
    subprocess.run([root / "scripts" / "test", suite.stem], check=True)


class McpClient:
    def __init__(
        self,
        binary: Path,
        arguments: tuple[str, ...] = (),
        environment: dict[str, str] | None = None,
    ) -> None:
        process = subprocess.Popen(
            [binary, *arguments],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        self.process = process
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr
        self.transcript: Transcript = []
        self.next_request_id = 1

    def send(self, message: dict[str, Any]) -> TranscriptEntry:
        recorded_message = message.copy()
        assert recorded_message.pop("jsonrpc", None) == "2.0", message

        entry = {}
        if "id" in recorded_message:
            entry["id"] = recorded_message.pop("id")
        params = recorded_message.get("params")
        if (
            recorded_message.keys() == {"method", "params"}
            and recorded_message["method"] == "tools/call"
            and isinstance(params, dict)
            and params.keys() == {"name", "arguments"}
            and params["name"] == "send"
            and isinstance(params["arguments"], dict)
        ):
            entry["send"] = params["arguments"]
        else:
            entry["input"] = recorded_message
        self.transcript.append(entry)
        self.stdin.write(json.dumps(message) + "\n")
        self.stdin.flush()
        return entry

    def receive(self, entry: TranscriptEntry) -> None:
        line = self.stdout.readline()
        assert line, "mcp-console stopped before replying"
        message = json.loads(line)
        assert message.pop("jsonrpc", None) == "2.0", message
        assert message.pop("id", None) == entry["id"], message
        assert message.keys() == {"result"} or message.keys() == {"error"}, message
        assert entry.keys().isdisjoint(message), message
        entry.update(message)

    def request(self, method: str, **params: Any) -> None:
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self.next_request_id,
            "method": method,
        }
        self.next_request_id += 1
        if params:
            message["params"] = params

        entry = self.send(message)
        self.receive(entry)

    def notify(self, method: str, **params: Any) -> None:
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params:
            message["params"] = params

        self.send(message)

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

    def call_tool(self, name: str, **arguments: Any) -> None:
        self.request(
            "tools/call",
            name=name,
            arguments=arguments,
        )

    def finish(self) -> Transcript:
        self.stdin.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            stdout = executor.submit(self.stdout.read)
            stderr = executor.submit(self.stderr.read)
            return_code = self.process.wait()
            extra_output = stdout.result()
            standard_error = stderr.result()

        assert return_code == 0, standard_error
        assert extra_output == "", f"unexpected extra output: {extra_output}"
        assert standard_error == "", standard_error

        return self.transcript


class WorkerClient:
    def __init__(self, binary: Path) -> None:
        worker_read, server_write = os.pipe()
        server_read, worker_write = os.pipe()
        environment = os.environ.copy()
        environment["MCP_CONSOLE_SIDEBAND_READ_FD"] = str(worker_read)
        environment["MCP_CONSOLE_SIDEBAND_WRITE_FD"] = str(worker_write)
        process = subprocess.Popen(
            [binary, "worker"],
            env=environment,
            pass_fds=(worker_read, worker_write),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        os.close(worker_read)
        os.close(worker_write)
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        self.process = process
        self.input = os.fdopen(server_write, "w", encoding="utf-8")
        self.output = os.fdopen(server_read, "r", encoding="utf-8")
        self.transcript: Transcript = []
        assert self.receive() == {"kind": "ready"}

    def send(self, message: TranscriptEntry) -> None:
        frame = json.dumps(message, separators=(",", ":"))
        self.transcript.append({"server": message.copy()})
        self.input.write(frame + "\n")
        self.input.flush()

    def receive(self) -> TranscriptEntry:
        line = self.output.readline()
        assert line, "mcp-console worker stopped before replying"
        frame = line.removesuffix("\n")
        message = json.loads(frame)
        assert isinstance(message, dict), message
        self._drain_standard_streams()
        self.transcript.append({"worker": message})
        return message

    def _drain_standard_streams(self) -> None:
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        streams = {
            self.process.stdout.fileno(): "stdout",
            self.process.stderr.fileno(): "stderr",
        }
        chunks: dict[str, list[bytes]] = {name: [] for name in streams.values()}

        while streams:
            ready, _, _ = select.select(streams, [], [], 0)
            if not ready:
                break
            for file_descriptor in ready:
                chunk = os.read(file_descriptor, 64 * 1024)
                if chunk:
                    chunks[streams[file_descriptor]].append(chunk)
                else:
                    streams.pop(file_descriptor)

        for name, stream_chunks in chunks.items():
            if stream_chunks:
                self.transcript.append({name: b"".join(stream_chunks).decode("utf-8")})

    def evaluate(self, language: str, source: str) -> None:
        self.send({"kind": "evaluate", "language": language, "source": source})
        while self.receive() != {"kind": "completed"}:
            pass

    def finish(self) -> Transcript:
        self.send({"kind": "shutdown"})
        self.input.close()
        stdout, stderr = self.process.communicate()
        self.output.close()
        if stdout:
            self.transcript.append({"stdout": stdout})
        if stderr:
            self.transcript.append({"stderr": stderr})
        self.transcript.append({"exit_code": self.process.returncode})
        assert self.process.returncode == 0, stderr
        return self.transcript
