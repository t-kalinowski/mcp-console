import base64
import json
import os
import select
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import (
    McpClient,
    ToolResult,
    Transcript,
    r_test_environment,
    run_this_suite,
    stop_client,
)

PLATFORMS = {"darwin"}

SCENARIO_ENV = "MCP_CONSOLE_TEST_RELAY_SCENARIO"

CAPTURE_NAME = "mcp-console-server-relay-wire.jsonl"

DONE_NAME = "mcp-console-scripted-relay-done"

EVALUATING_NAME = "mcp-console-scripted-relay-evaluating"

CHECKPOINT_NAME = "mcp-console-scripted-relay-checkpoint"

RELEASE_NAME = "mcp-console-scripted-relay-release"

STDIN_FAILURE_RELEASED_NAME = "mcp-console-stdin-failure-released"

STDIN_FAILURE_RELEASED_ENV = "MCP_CONSOLE_TEST_STDIN_FAILURE_RELEASED"

SHUTDOWN_RECEIVED_NAME = "mcp-console-scripted-relay-shutdown-received"

RETIREMENT_RELEASE_NAME = "mcp-console-scripted-relay-retirement-release"

PRELUDE_RELEASE_NAME = "mcp-console-scripted-relay-prelude-release"

PRELUDE_PROCESSED_NAME = "mcp-console-scripted-relay-prelude-processed"

EVALUATION_OUTPUT_READY_NAME = "mcp-console-scripted-relay-evaluation-output-ready"

PREPARATION_RECEIVED_NAME = "mcp-console-scripted-relay-preparation-received"

PREPARATION_RESULT_RELEASE_NAME = (
    "mcp-console-scripted-relay-preparation-result-release"
)

PREPARATION_RESULT_SENT_NAME = "mcp-console-scripted-relay-preparation-result-sent"

R_PREPARATION_RESOLVE_CHECKPOINT_NAME = "mcp-console-r-preparation-resolve-checkpoint"

R_PREPARATION_RESOLVE_RELEASE_NAME = "mcp-console-r-preparation-resolve-release"

IDLE_R_RESOLUTION_READY_NAME = "mcp-console-idle-r-resolution-ready"

IDLE_R_RESOLUTION_RELEASE_NAME = "mcp-console-idle-r-resolution-release"

IDLE_R_EVALUATION_RECEIVED_NAME = "mcp-console-idle-r-evaluation-received"

EXPLICIT_R_PREPARATION_CALLBACK_NAME = "mcp-console-explicit-r-preparation-callback"

EXPLICIT_R_PREPARATION_CALLBACK_REPLY_NAME = (
    "mcp-console-explicit-r-preparation-callback-reply"
)

INTERRUPT_ACTIVE_RELEASE_NAME = "mcp-console-interrupt-active-release"

INTERRUPT_ACK_RELEASE_NAME = "mcp-console-interrupt-ack-release"

INTERRUPT_ACKNOWLEDGED_NAME = "mcp-console-interrupt-acknowledged"

INTERRUPT_RECEIVED_NAME = "mcp-console-interrupt-received"

CONTROLLED_COMPLETION_RELEASE_NAME = "mcp-console-controlled-completion-release"

CONTROLLED_COMPLETION_SENT_NAME = "mcp-console-controlled-completion-sent"

RESTART_REQUIREMENTS_CHECK_NAME = "mcp-console-restart-requirements-check"

RESTART_REQUIREMENTS_CHECKED_NAME = "mcp-console-restart-requirements-checked"

RESTART_REQUIREMENTS_RESOLVED_NAME = "mcp-console-restart-requirements-resolved"

RESTART_REQUIREMENTS_EVALUATION_RECEIVED_NAME = (
    "mcp-console-restart-requirements-evaluation-received"
)

RESTART_REQUIREMENTS_EVALUATION_RELEASE_NAME = (
    "mcp-console-restart-requirements-evaluation-release"
)

PENDING_TEXT_BUDGET = 8 * 1024 * 1024

PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)


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


def _ordered_input_barrier(client: McpClient) -> None:
    barrier = client._request("ping")
    assert barrier["result"] == {}, barrier


class ServerRelayClient:
    def __init__(
        self,
        binary: Path,
        scenario: str,
        environment: dict[str, str] | None = None,
    ) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        environment = os.environ.copy() if environment is None else environment.copy()
        environment["TMPDIR"] = str(self.root)
        environment[SCENARIO_ENV] = scenario
        if scenario == "stdin_forwarding_failure":
            environment[STDIN_FAILURE_RELEASED_ENV] = str(
                self.root / STDIN_FAILURE_RELEASED_NAME
            )
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

    def start_worker(self) -> None:
        assert _tool_text(self.send(control="restart")) == (
            "[starting new worker]\n[idle]"
        )

    def send(self, **arguments: object) -> ToolResult:
        return self.client.send(**arguments)

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

    def relay_root(self) -> Path:
        return self._capture_path().parent

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
            assert allow_raw and entry.keys() in ({"server_raw"}, {"relay_raw"}), entry
            base64.b64decode(next(iter(entry.values())), validate=True)
        return transcript


class FifoCheckpoint:
    def __init__(self, path: Path, *, create: bool = False) -> None:
        self.path = path
        if create:
            os.mkfifo(path)
        # Keep a writer open so an early release cannot strand a later reader
        # in its blocking open.
        self.descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def close(self) -> None:
        os.close(self.descriptor)

    def wait(self) -> None:
        readable, _, _ = select.select([self.descriptor], [], [], 10)
        assert readable, f"checkpoint was not reached: {self.path.name}"
        assert os.read(self.descriptor, 1) == b"1"

    def release(self) -> None:
        assert os.write(self.descriptor, b"1") == 1


def _fake_ir_environment(
    root: Path,
    libraries: list[Path],
) -> dict[str, str]:
    environment, _ = r_test_environment()
    fake_bin = root / "bin"
    fake_bin.mkdir()
    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "ordered_retirement_ir"
    (fake_bin / "ir").symlink_to(fixture)
    path = environment.get("PATH")
    assert path is not None, "PATH is required"
    environment["PATH"] = os.pathsep.join((str(fake_bin), path))
    environment["MCP_CONSOLE_TEST_IR_COUNTER"] = str(root / "ir-counter")
    environment["MCP_CONSOLE_TEST_IR_LIBRARIES"] = os.pathsep.join(map(str, libraries))
    return environment


def _normalize_shutdown_grace(transcript: Transcript) -> list[dict[str, Any]]:
    shutdown = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "shutdown"
    ]
    for command in shutdown:
        grace_millis = command["grace_millis"]
        assert isinstance(grace_millis, int) and 0 <= grace_millis <= 1_000, command
        command["grace_millis"] = "<remaining shutdown grace>"
    return shutdown


def _receive_checkpointed(
    client: McpClient,
    entry: dict[str, Any],
    description: str,
) -> None:
    readable, _, _ = select.select([client.stdout], [], [], 10)
    assert readable, f"mcp-console did not return {description}"
    client._receive(entry)


def _wait_for_recorded_tool_result(
    client: McpClient,
    entry: dict[str, Any],
) -> dict[str, Any]:
    assert client.temporary_directory is not None
    workspace = Path(client.temporary_directory.name)
    session = next((workspace / ".mcp-console" / "sessions").iterdir())
    journal = session / "internal" / "events.jsonl"
    with journal.open(encoding="utf-8") as journal_stream:
        journal_events = select.kqueue()
        journal_events.control(
            [
                select.kevent(
                    journal_stream.fileno(),
                    filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_WRITE,
                )
            ],
            0,
            0,
        )
        try:
            while True:
                journal_stream.seek(0)
                events = [json.loads(line) for line in journal_stream]
                call = next(
                    (
                        event
                        for event in events
                        if event["event"] == "tool_call"
                        and event["request_id"] == entry["id"]
                    ),
                    None,
                )
                result = next(
                    (
                        event["result"]
                        for event in events
                        if call is not None
                        and event["event"] == "tool_result"
                        and event["call_id"] == call["call_id"]
                    ),
                    None,
                )
                if result is not None:
                    return result
                assert client.process.poll() is None, (
                    "mcp-console stopped before recording the tool result"
                )
                assert journal_events.control(None, 1, 10), (
                    "mcp-console did not record the tool result"
                )
        finally:
            journal_events.close()


def _reports_worker_outcome(
    binary: Path,
    scenario: str,
    diagnostic: str,
) -> tuple[Transcript, str]:
    client = ServerRelayClient(binary, scenario)
    failed = client.client._start_send(r="42")
    transcript = client.release_failure(failed, diagnostic)
    result = failed["result"]
    assert result.get("isError") is True, result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    output = content[0]["text"]
    stopped = "[worker stopped: in-memory state lost]"
    replacement = "[starting new worker]"
    assert (
        output.index(diagnostic) < output.index(stopped) < output.index(replacement)
    ), output
    return transcript, output


__all__ = [name for name in globals() if name not in {"__builtins__"}]
