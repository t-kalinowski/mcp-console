import json
import subprocess
from pathlib import Path
from typing import Any


Transcript = list[dict[str, dict[str, Any]]]


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
        self.messages: list[dict[str, dict[str, Any]]] = []
        self.next_request_id = 1

    def send(self, message: dict[str, Any]) -> None:
        self.messages.append({"input": message})
        self.stdin.write(json.dumps(message) + "\n")
        self.stdin.flush()

    def receive(self) -> None:
        line = self.stdout.readline()
        assert line, "mcp-console stopped before replying"
        self.messages.append({"output": json.loads(line)})

    def request(self, method: str, **params: Any) -> None:
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self.next_request_id,
            "method": method,
        }
        self.next_request_id += 1
        if params:
            message["params"] = params

        self.send(message)
        self.receive()

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

    def console(self, **arguments: Any) -> None:
        self.call_tool("console", **arguments)

    def finish(self) -> Transcript:
        self.stdin.close()
        extra_output = self.stdout.read()
        standard_error = self.stderr.read()
        return_code = self.process.wait()

        assert return_code == 0, standard_error
        assert extra_output == "", f"unexpected extra output: {extra_output}"
        assert standard_error == "", standard_error

        return self.messages
