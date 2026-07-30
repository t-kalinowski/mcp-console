import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


TranscriptEntry = dict[str, Any]
Transcript = list[TranscriptEntry]


def run_this_suite(suite_path: str) -> None:
    suite = Path(suite_path).resolve()
    root = suite.parents[2]
    subprocess.run([root / "scripts" / "test", suite.stem], check=True)


class McpClient:
    def __init__(
        self,
        binary: Path,
        arguments: tuple[str, ...] = (),
    ) -> None:
        process = subprocess.Popen(
            [binary, *arguments],
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
        entry["output"] = message

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
