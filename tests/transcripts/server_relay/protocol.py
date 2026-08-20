#!/usr/bin/env -S uv run --script

import base64
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import McpClient, ToolResult, Transcript, run_this_suite

PLATFORMS = {"darwin"}
SCENARIO_ENV = "MCP_CONSOLE_TEST_RELAY_SCENARIO"
CAPTURE_NAME = "mcp-console-server-relay-wire.jsonl"
DONE_NAME = "mcp-console-scripted-relay-done"
EVALUATING_NAME = "mcp-console-scripted-relay-evaluating"
CHECKPOINT_NAME = "mcp-console-scripted-relay-checkpoint"
RELEASE_NAME = "mcp-console-scripted-relay-release"


def _tool_text(result: ToolResult) -> str:
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    return content[0]["text"]


def _tool_error(entry: dict[str, Any], expected: str) -> None:
    result = entry["result"]
    assert result.get("isError") is True, result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    assert expected in content[0]["text"], content


class ServerRelayClient:
    def __init__(self, binary: Path, scenario: str) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        environment = os.environ.copy()
        environment["TMPDIR"] = str(self.root)
        environment[SCENARIO_ENV] = scenario
        relay = Path(__file__).resolve().parents[2] / "fixtures" / "scripted_relay"
        self.client = McpClient(
            binary,
            (
                "serve",
                "--worker",
                str(binary),
                "--relay",
                str(relay),
            ),
            environment,
        )
        self.client._initialize_and_list_tools()

    def send(self, **arguments: object) -> ToolResult:
        return self.client.send(**arguments)

    def session(self, **arguments: object) -> ToolResult:
        return self.client.session(**arguments)

    def finish_active(self) -> Transcript:
        self._wait_for(DONE_NAME)
        transcript = self._read_capture(self._capture_path())
        self.client._finish()
        self._temporary.cleanup()
        return transcript

    def finish_shutdown(self) -> Transcript:
        self._wait_for(DONE_NAME)
        capture_path = self._capture_path()
        with capture_path.open(encoding="utf-8") as capture:
            self.client._finish()
            transcript = self._read_open_capture(capture)
        self._temporary.cleanup()

        shutdown = [
            entry["server"]
            for entry in transcript
            if entry.keys() == {"server"} and entry["server"].get("kind") == "shutdown"
        ]
        assert len(shutdown) == 1, shutdown
        grace_millis = shutdown[0]["grace_millis"]
        assert isinstance(grace_millis, int) and 0 <= grace_millis <= 1_000, shutdown
        shutdown[0]["grace_millis"] = "<remaining shutdown grace>"
        return transcript

    def release_failure(self, entry: dict[str, Any], expected: str) -> Transcript:
        checkpoint = self._wait_for(CHECKPOINT_NAME)
        capture_path = checkpoint.with_name(CAPTURE_NAME)
        assert capture_path.is_file(), capture_path
        with capture_path.open(encoding="utf-8") as capture:
            checkpoint.with_name(RELEASE_NAME).touch()
            self.client._receive(entry)
            _tool_error(entry, expected)
            self.client._finish()
            transcript = self._read_open_capture(capture, allow_raw=True)
        self._temporary.cleanup()
        return transcript

    def _capture_path(self) -> Path:
        paths = list(self.root.glob(f"mcp-console-tmp-*/{CAPTURE_NAME}"))
        assert len(paths) == 1, paths
        return paths[0]

    def _wait_for(self, name: str) -> Path:
        deadline = time.monotonic() + 10
        while True:
            paths = list(self.root.glob(f"mcp-console-tmp-*/{name}"))
            assert len(paths) <= 1, paths
            if paths:
                return paths[0]
            assert self.client.process.poll() is None, (
                f"mcp-console stopped before scripted relay created {name}"
            )
            assert time.monotonic() < deadline, f"scripted relay did not create {name}"
            time.sleep(0.01)

    @staticmethod
    def _read_capture(capture: Path) -> Transcript:
        with capture.open(encoding="utf-8") as stream:
            return ServerRelayClient._read_open_capture(stream)

    @staticmethod
    def _read_open_capture(
        capture: TextIO,
        *,
        allow_raw: bool = False,
    ) -> Transcript:
        transcript = [json.loads(line) for line in capture.read().splitlines()]
        for entry in transcript:
            if entry.keys() in ({"server"}, {"relay"}):
                message = next(iter(entry.values()))
                assert isinstance(message, dict), entry
                continue
            assert allow_raw and entry.keys() == {"relay_raw"}, entry
            base64.b64decode(entry["relay_raw"], validate=True)
        return transcript


def test_starts_and_reports_ready(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "ready")
    assert _tool_text(client.session(action="restart")) == (
        "[starting new worker]\n[idle]"
    )
    return client.finish_active()


def test_evaluates_and_commits_terminal(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "evaluate")
    assert _tool_text(client.send(r="42")) == "[done]"
    return client.finish_active()


def test_forwards_raw_stdout_and_stderr(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "raw_output")
    assert _tool_text(client.send(r="42")) == "stdout\nstderr\n"
    transcript = client.finish_active()

    output = [
        entry["relay"]
        for entry in transcript
        if entry.keys() == {"relay"}
        and entry["relay"].get("kind") in {"stdout", "stderr"}
    ]
    assert [base64.b64decode(event["data"], validate=True) for event in output] == [
        b"stdout\n",
        b"stderr\n",
    ]
    return transcript


def test_forwards_stdin(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "stdin")
    assert _tool_text(client.send(r="42", stdin="answer\n")) == "[done]"
    return client.finish_active()


def test_interrupts_and_reports_result(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "interrupt")
    evaluation = client.client._start_send(r="42")
    client._wait_for(EVALUATING_NAME)
    interrupt = client.client._start_session(action="interrupt")
    client.client._receive_many([evaluation, interrupt])
    assert _tool_text(evaluation["result"]) == "[done]"
    assert _tool_text(interrupt["result"]) == "[interrupt sent]"
    return client.finish_active()


def test_gracefully_shuts_down(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "shutdown")
    assert _tool_text(client.session(action="restart")) == (
        "[starting new worker]\n[idle]"
    )
    return client.finish_shutdown()


def test_reports_fatal_failure(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "fatal")
    failed = client.client._start_send(r="42")
    transcript = client.release_failure(failed, "scripted relay failure")
    assert transcript[-1] == {"relay": {"kind": "worker_exited"}}, transcript
    return transcript


def test_rejects_truncated_output(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "truncated")
    failed = client.client._start_send(r="42")
    transcript = client.release_failure(
        failed,
        "relay stream closed midway through a frame",
    )
    assert len(transcript) == 1, transcript
    raw = base64.b64decode(transcript[0]["relay_raw"], validate=True)
    assert raw == b'{"kind":"worker_message"', raw
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
