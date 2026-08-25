#!/usr/bin/env -S uv run --script

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
CONTROLLED_INTERRUPT_FIRST_RECEIVED_NAME = (
    "mcp-console-controlled-interrupt-first-received"
)
CONTROLLED_INTERRUPT_SECOND_RECEIVED_NAME = (
    "mcp-console-controlled-interrupt-second-received"
)
CONTROLLED_INTERRUPT_EVALUATION_RELEASE_NAME = (
    "mcp-console-controlled-interrupt-evaluation-release"
)
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
        assert _tool_text(self.session(action="restart")) == (
            "[starting new worker]\n[idle]"
        )

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
        self.descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    def close(self) -> None:
        os.close(self.descriptor)

    def wait(self) -> None:
        readable, _, _ = select.select([self.descriptor], [], [], 10)
        assert readable, f"checkpoint was not reached: {self.path.name}"
        assert os.read(self.descriptor, 1) == b"1"

    def release(self) -> None:
        descriptor = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            write = os.write(descriptor, b"1")
            assert write == 1
        finally:
            os.close(descriptor)


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


def test_starts_and_reports_ready(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "ready")
    assert _tool_text(client.session(action="restart")) == (
        "[starting new worker]\n[idle]"
    )
    return client.finish_active()


def test_evaluates_and_commits_operation_result(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "evaluate")
    assert _tool_text(client.send(r="42")) == "[done]"
    transcript = client.finish_active()
    assert not any(
        entry.keys() == {"server"}
        and entry["server"].get("kind") == "terminal_committed"
        for entry in transcript
    ), transcript
    return transcript


def test_forwards_raw_stdout_and_stderr(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "raw_output")
    assert _tool_text(client.send(r="42")) == (
        "stdout text 👩🏽‍💻\nstderr text\n�stdout bytes\n�stderr bytes\n"
    )
    transcript = client.finish_active()

    output = [
        entry["relay"]
        for entry in transcript
        if entry.keys() == {"relay"}
        and entry["relay"].get("kind")
        in {"stdout", "stderr", "stdout_bytes", "stderr_bytes"}
    ]
    assert output[:2] == [
        {"kind": "stdout", "data": "stdout text 👩🏽‍💻\n"},
        {"kind": "stderr", "data": "stderr text\n"},
    ]
    assert [base64.b64decode(event["data"], validate=True) for event in output[2:]] == [
        b"\xffstdout bytes\n",
        b"\xfestderr bytes\n",
    ]
    return transcript


def test_malformed_byte_completes_pending_redraw(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "raw_malformed_redraw")
    assert _tool_text(client.send(r="42")) == "�\n"
    return client.finish_active()


def test_forwards_stdin(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "stdin")
    assert _tool_text(client.send(r="42", stdin="answer\n")) == "[done]"
    return client.finish_active()


def test_empty_stdin_sends_no_relay_command(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "evaluate")
    assert _tool_text(client.send(r="42", stdin="")) == "[done]"
    return client.finish_active()


def test_prepares_initial_requirements_before_stdin_and_skips_retained_resolution(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "initial-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        client = ServerRelayClient(
            binary,
            "initial_requirements_stdin_idempotent",
            environment,
        )

        assert (
            _tool_text(
                client.send(
                    python="42",
                    stdin="answer\n",
                    requirements={"r": ["initial-requirement"]},
                )
            )
            == "[done]"
        )
        assert (
            _tool_text(
                client.send(
                    r="43",
                    requirements={"r": ["initial-requirement"]},
                )
            )
            == "[done]"
        )
        transcript = client.finish_active()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    server_commands = [
        entry["server"] for entry in transcript if entry.keys() == {"server"}
    ]
    assert [command["kind"] for command in server_commands[:3]] == [
        "stdin",
        "evaluate",
        "evaluate",
    ], server_commands
    assert not any(command["kind"] == "prepare_r" for command in server_commands)
    return transcript


def test_send_timeout_starts_after_blocked_requirements_resolver(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "timeout-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        resolver_started = FifoCheckpoint(root / "resolver-started", create=True)
        resolver_release = FifoCheckpoint(root / "resolver-release", create=True)
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        client = ServerRelayClient(
            binary,
            "live_r_requirements_then_evaluate",
            environment,
        )
        client.start_worker()
        finished = False
        try:
            evaluation = client.client._start_send(
                r="42",
                requirements={"r": ["timeout-requirement"]},
                timeout_ms=50,
            )
            resolver_started.wait()
            readable, _, _ = select.select([client.client.stdout], [], [], 0.25)
            assert not readable, (
                "send timeout applied while requirements were resolving"
            )

            resolver_release.release()
            _receive_checkpointed(
                client.client,
                evaluation,
                "the evaluation after requirement resolution",
            )
            assert _tool_text(evaluation["result"]) == "[done]"
            transcript = client.finish_active()
            finished = True
        finally:
            if not finished:
                stop_client(client.client)
                client._temporary.cleanup()
            resolver_started.close()
            resolver_release.close()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    prepare_commands = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "prepare_r"
    ]
    assert prepare_commands == [{"kind": "prepare_r", "library": str(library)}]
    prepare_commands[0]["library"] = "<timeout-candidate>"
    prepared_events = [
        entry["relay"]
        for entry in transcript
        if entry.keys() == {"relay"} and entry["relay"].get("kind") == "r_prepared"
    ]
    assert prepared_events == [{"kind": "r_prepared", "library": str(library)}]
    prepared_events[0]["library"] = "<timeout-candidate>"
    evaluations = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "evaluate"
    ]
    assert evaluations == [{"kind": "evaluate", "language": "r", "source": "42"}]
    return transcript


def test_stdin_forwarding_failure_does_not_execute_cell(
    binary: Path,
) -> Transcript:
    client = ServerRelayClient(binary, "stdin_forwarding_failure")
    client.start_worker()
    relay_root = client.relay_root()
    capture_path = relay_root / CAPTURE_NAME
    finished = False
    with capture_path.open(encoding="utf-8") as capture:
        try:
            evaluation = client.client._start_send(
                r="must not execute",
                stdin="x" * (4 * 1024 * 1024),
            )
            checkpoint = client._wait_for(CHECKPOINT_NAME)
            (client.root / STDIN_FAILURE_RELEASED_NAME).touch()
            checkpoint.with_name(RELEASE_NAME).touch()
            client.client._receive(evaluation)
            result = evaluation["result"]
            assert result.get("isError") is True, result
            output = result["content"][0]["text"]
            assert "worker relay stdin write failed" in output, output
            assert output.endswith(
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            ), output

            transcript = client._read_open_capture(capture, allow_raw=True)
            assert transcript[0] == {"relay": {"kind": "ready"}}, transcript
            assert transcript[1].keys() == {"server_raw"}, transcript
            raw = base64.b64decode(transcript[1]["server_raw"], validate=True)
            assert raw.startswith(b'{"kind":"stdin","data":"'), raw
            transcript[1]["server_raw"] = "<partial stdin frame>"
            assert not any(entry.keys() == {"server"} for entry in transcript), (
                transcript
            )

            assert _tool_text(client.send(r="42")) == "[done]"
            captures = list(client.root.glob(f"mcp-console-tmp-*/{CAPTURE_NAME}"))
            assert len(captures) == 1 and captures[0] != capture_path, (
                capture_path,
                captures,
            )
            replacement_capture_path = captures[0]
            with replacement_capture_path.open(encoding="utf-8") as replacement:
                client.client._finish()
                transcript.extend(client._read_open_capture(replacement))
            finished = True
            return transcript
        finally:
            if not finished:
                stop_client(client.client)
            client._temporary.cleanup()


def test_interrupts_and_reports_result(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "interrupt")
    evaluation = client.client._start_send(r="42")
    client._wait_for(EVALUATING_NAME)
    interrupt = client.client._start_session(action="interrupt")
    client.client._receive_many([evaluation, interrupt])
    assert _tool_text(evaluation["result"]) == "[done]"
    assert _tool_text(interrupt["result"]) == "[interrupt sent]"
    return client.finish_active()


def test_controlled_restart_routes_stdin_and_cell_to_replacement(
    binary: Path,
) -> Transcript:
    client = ServerRelayClient(binary, "controlled_restart_stdin")
    finished = False
    old_capture = None
    try:
        client.start_worker()
        old_root = client.relay_root()
        old_capture = (old_root / CAPTURE_NAME).open(encoding="utf-8")

        assert _tool_text(client.send(stdin="stale\n")) == "\n[idle]"
        result = client.send(
            control="restart",
            stdin="fresh\n",
            r="replacement cell",
        )
        output = _tool_text(result)
        stopped = output.index("[worker stopped: in-memory state lost]")
        starting = output.index("[starting new worker]")
        consumed = "replacement cell consumed stdin: fresh\n"
        evaluated = output.index(consumed)
        assert stopped < starting < evaluated, output
        assert output.count(consumed) == 1, output

        old_transcript = client._read_open_capture(old_capture)
        replacement_transcript = client.finish_active()
        finished = True
    finally:
        if old_capture is not None:
            old_capture.close()
        if not finished:
            stop_client(client.client)
            client._temporary.cleanup()

    old_commands = [
        entry["server"] for entry in old_transcript if entry.keys() == {"server"}
    ]
    assert old_commands[0] == {"kind": "stdin", "data": "stale\n"}, old_commands
    assert len(old_commands) == 2 and old_commands[1]["kind"] == "shutdown", (
        old_commands
    )
    replacement_commands = [
        entry["server"]
        for entry in replacement_transcript
        if entry.keys() == {"server"}
    ]
    assert replacement_commands == [
        {"kind": "stdin", "data": "fresh\n"},
        {
            "kind": "evaluate",
            "language": "r",
            "source": "replacement cell",
        },
    ], replacement_commands
    assert len(_normalize_shutdown_grace(old_transcript)) == 1
    return old_transcript + replacement_transcript


def test_controlled_restart_with_stdin_only_reports_replacement_idle(
    binary: Path,
) -> Transcript:
    client = ServerRelayClient(binary, "controlled_restart_stdin_only")
    result = client.send(control="restart", stdin="replacement input\n")
    assert _tool_text(result) == "[starting new worker]\n[idle]", result
    transcript = client.finish_active()

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [{"kind": "stdin", "data": "replacement input\n"}], commands
    return transcript


def test_control_only_interrupt_preserves_controlled_completion_marker(
    binary: Path,
) -> Transcript:
    client = ServerRelayClient(binary, "controlled_completion_then_interrupt")
    result = client.send(
        control="restart",
        r="controlled cell completed before later interrupt",
        timeout_ms=0,
    )
    assert _tool_text(result).endswith("[running; poll with an empty send]"), result

    relay_root = client.relay_root()
    completion_release = FifoCheckpoint(relay_root / CONTROLLED_COMPLETION_RELEASE_NAME)
    completion_sent = FifoCheckpoint(relay_root / CONTROLLED_COMPLETION_SENT_NAME)
    finished = False
    released = False
    try:
        completion_release.release()
        released = True
        completion_sent.wait()

        result = client.send(control="interrupt", timeout_ms=3_000)
        assert _tool_text(result) == (
            "controlled cell completed before later interrupt\n[done]"
        )
        transcript = client.finish_active()
        finished = True
    finally:
        if not released:
            completion_release.release()
        completion_release.close()
        completion_sent.close()
        if not finished:
            stop_client(client.client)
            client._temporary.cleanup()

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands[0] == {
        "kind": "evaluate",
        "language": "r",
        "source": "controlled cell completed before later interrupt",
    }, commands
    assert commands[1] == {"kind": "interrupt", "request_id": 0}, commands
    assert len(commands) == 2, commands
    return transcript


def test_controlled_restart_resolves_requirements_before_replacement_and_timeout(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "restart-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        resolver_started = FifoCheckpoint(root / "resolver-started", create=True)
        resolver_release = FifoCheckpoint(root / "resolver-release", create=True)
        resolver_finished = FifoCheckpoint(root / "resolver-finished", create=True)
        resolver_finish_release = FifoCheckpoint(
            root / "resolver-finish-release", create=True
        )
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        environment["MCP_CONSOLE_TEST_IR_FINISHED"] = str(resolver_finished.path)
        environment["MCP_CONSOLE_TEST_IR_FINISH_RELEASE"] = str(
            resolver_finish_release.path
        )
        client = ServerRelayClient(
            binary,
            "controlled_restart_requirements",
            environment,
        )
        client.start_worker()
        old_root = client.relay_root()
        old_capture = (old_root / CAPTURE_NAME).open(encoding="utf-8")
        requirement_check = FifoCheckpoint(old_root / RESTART_REQUIREMENTS_CHECK_NAME)
        requirement_checked = FifoCheckpoint(
            old_root / RESTART_REQUIREMENTS_CHECKED_NAME
        )
        requirement_resolved = FifoCheckpoint(
            old_root / RESTART_REQUIREMENTS_RESOLVED_NAME
        )
        resolver_released = False
        requirement_resolution_reported = False
        resolver_finish_released = False
        replacement_evaluation_received = None
        replacement_evaluation_release = None
        replacement_evaluation_released = False
        finished = False
        try:
            evaluation = client.client._start_send(
                control="restart",
                r="replacement requirement cell",
                requirements={"r": ["restart-requirement"]},
                timeout_ms=1_000,
            )
            resolver_started.wait()
            requirement_check.release()
            requirement_checked.wait()

            readable, _, _ = select.select([client.client.stdout], [], [], 1.25)
            assert not readable, (
                "send timeout applied while restart requirements were resolving"
            )

            resolver_release.release()
            resolver_released = True
            resolver_finished.wait()
            requirement_resolved.release()
            requirement_resolution_reported = True
            resolver_finish_release.release()
            resolver_finish_released = True
            evaluation_received_path = client._wait_for(
                RESTART_REQUIREMENTS_EVALUATION_RECEIVED_NAME
            )
            replacement_evaluation_received = FifoCheckpoint(evaluation_received_path)
            replacement_evaluation_release = FifoCheckpoint(
                evaluation_received_path.with_name(
                    RESTART_REQUIREMENTS_EVALUATION_RELEASE_NAME
                )
            )
            replacement_evaluation_received.wait()
            readable, _, _ = select.select([client.client.stdout], [], [], 0.2)
            assert not readable, (
                "send timeout expired before the replacement evaluation's fresh "
                "wait budget"
            )
            replacement_evaluation_release.release()
            replacement_evaluation_released = True
            _receive_checkpointed(
                client.client,
                evaluation,
                "the controlled evaluation after restart requirement resolution",
            )
            assert _tool_text(evaluation["result"]) == (
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "replacement requirement cell ran\n"
                "[done]"
            )
            old_transcript = client._read_open_capture(old_capture)
            replacement_transcript = client.finish_active()
            finished = True
        finally:
            if not resolver_released:
                resolver_release.release()
            if not requirement_resolution_reported:
                requirement_resolved.release()
            if not resolver_finish_released:
                resolver_finish_release.release()
            if (
                replacement_evaluation_release is not None
                and not replacement_evaluation_released
            ):
                replacement_evaluation_release.release()
            if not finished:
                stop_client(client.client)
                client._temporary.cleanup()
            old_capture.close()
            requirement_check.close()
            requirement_checked.close()
            requirement_resolved.close()
            resolver_started.close()
            resolver_release.close()
            resolver_finished.close()
            resolver_finish_release.close()
            if replacement_evaluation_received is not None:
                replacement_evaluation_received.close()
            if replacement_evaluation_release is not None:
                replacement_evaluation_release.close()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    old_commands = [
        entry["server"] for entry in old_transcript if entry.keys() == {"server"}
    ]
    assert len(old_commands) == 1 and old_commands[0]["kind"] == "shutdown", (
        old_commands
    )
    replacement_commands = [
        entry["server"]
        for entry in replacement_transcript
        if entry.keys() == {"server"}
    ]
    assert replacement_commands == [
        {
            "kind": "evaluate",
            "language": "r",
            "source": "replacement requirement cell",
        }
    ], replacement_commands
    assert not any(
        command["kind"] == "prepare_r"
        for command in old_commands + replacement_commands
    )
    assert len(_normalize_shutdown_grace(old_transcript)) == 1
    return old_transcript + replacement_transcript


def test_controlled_restart_requirement_failure_preserves_old_worker(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "unused-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        environment["MCP_CONSOLE_TEST_IR_FAILURE"] = (
            "synthetic controlled restart requirement failure"
        )
        client = ServerRelayClient(binary, "ready", environment)
        client.start_worker()
        old_capture = (client.relay_root() / CAPTURE_NAME).open(encoding="utf-8")
        finished = False
        try:
            result = client.send(
                control="restart",
                stdin="must not send\n",
                r="must not evaluate",
                requirements={"r": ["failing-restart-requirement"]},
            )
            assert result == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[R package resolution failed with exit status: 1: "
                            "synthetic controlled restart requirement failure]"
                        ),
                    }
                ],
                "isError": True,
            }, result
            assert _tool_text(client.send()) == "\n[idle]"

            before_cleanup = client._read_open_capture(old_capture)
            assert not any(entry.keys() == {"server"} for entry in before_cleanup), (
                before_cleanup
            )
            transcript = client.finish_active()
            finished = True
        finally:
            if not finished:
                stop_client(client.client)
                client._temporary.cleanup()
            old_capture.close()

    server_commands = [
        entry["server"] for entry in transcript if entry.keys() == {"server"}
    ]
    assert server_commands == [], server_commands
    return transcript


def test_controlled_interrupt_orders_stdin_before_new_evaluation(
    binary: Path,
) -> Transcript:
    client = ServerRelayClient(binary, "controlled_interrupt_stdin_evaluate")
    client.send(r="old evaluation", timeout_ms=0)
    assert _tool_text(client.client.transcript[-1]["result"]) == (
        "\n[running; poll with an empty send]"
    )
    client._wait_for(EVALUATING_NAME)

    result = client.send(
        control="interrupt",
        stdin="finish old\n",
        r="new evaluation",
        timeout_ms=50,
    )
    output = _tool_text(result)
    old = output.index("old evaluation finished from stdin\n")
    new = output.index("new evaluation ran\n")
    assert old < new, output
    assert output.count("old evaluation finished from stdin\n") == 1, output
    assert output.count("new evaluation ran\n") == 1, output
    assert output.endswith("[done]"), output

    transcript = client.finish_active()
    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation",
        },
        {"kind": "interrupt", "request_id": 0},
        {"kind": "stdin", "data": "finish old\n"},
        {
            "kind": "evaluate",
            "language": "r",
            "source": "new evaluation",
        },
    ], commands
    return transcript


def test_controlled_interrupt_stdin_precedes_failing_requirements_without_new_cell(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "interrupt-failure-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        client = ServerRelayClient(
            binary,
            "controlled_interrupt_stdin_requirement_failure",
            environment,
        )
        client.send(
            r="old evaluation before failing requirements",
            timeout_ms=0,
        )
        assert _tool_text(client.client.transcript[-1]["result"]) == (
            "\n[running; poll with an empty send]"
        )
        client._wait_for(EVALUATING_NAME)

        result = client.send(
            control="interrupt",
            stdin="finish old before preparation\n",
            r="new evaluation must not run after preparation failure",
            requirements={"r": ["failing-after-interrupt"]},
        )
        assert result.get("isError") is True, result
        text = "".join(
            content["text"]
            for content in result["content"]
            if content["type"] == "text"
        )
        prior = text.index("old evaluation finished before preparation\n")
        failure = text.index(
            "scripted controlled interrupt R preparation failed; further "
            "requirement changes are unavailable until session restart"
        )
        assert prior < failure, text

        transcript = client.finish_active()
        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation before failing requirements",
        },
        {"kind": "interrupt", "request_id": 0},
        {"kind": "stdin", "data": "finish old before preparation\n"},
        {"kind": "prepare_r", "library": str(library)},
    ], commands
    commands[-1]["library"] = "<interrupt-failure-candidate>"
    return transcript


def test_controlled_interrupt_stdin_precedes_invalid_requirements_without_new_cell(
    binary: Path,
) -> Transcript:
    client = ServerRelayClient(
        binary,
        "controlled_interrupt_stdin_invalid_requirements",
    )
    client.send(
        r="old evaluation before invalid requirements",
        timeout_ms=0,
    )
    assert _tool_text(client.client.transcript[-1]["result"]) == (
        "\n[running; poll with an empty send]"
    )
    client._wait_for(EVALUATING_NAME)

    result = client.send(
        control="interrupt",
        stdin="finish old before validation\n",
        r="new evaluation must not run after validation failure",
        requirements={},
    )
    assert result.get("isError") is True, result
    text = "".join(
        content["text"] for content in result["content"] if content["type"] == "text"
    )
    prior = text.index("old evaluation finished before validation\n")
    validation = text.index(
        "at least one of `requirements.r`, `requirements.python`, or "
        "`requirements.duckdb` is required"
    )
    assert prior < validation, text

    transcript = client.finish_active()
    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation before invalid requirements",
        },
        {"kind": "interrupt", "request_id": 0},
        {"kind": "stdin", "data": "finish old before validation\n"},
    ], commands
    return transcript


def test_standalone_interrupt_targets_blocked_controlled_restart_resolver(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "unused-interrupted-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        resolver_started = FifoCheckpoint(root / "resolver-started", create=True)
        resolver_release = FifoCheckpoint(root / "resolver-release", create=True)
        resolver_interrupted = FifoCheckpoint(
            root / "resolver-interrupted", create=True
        )
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        environment["MCP_CONSOLE_TEST_IR_INTERRUPTED"] = str(resolver_interrupted.path)
        client = ServerRelayClient(binary, "ready", environment)
        client.start_worker()
        capture = (client.relay_root() / CAPTURE_NAME).open(encoding="utf-8")
        resolver_released = False
        finished = False
        try:
            controlled = client.client._start_send(
                control="restart",
                r="cell must not run after resolver interrupt",
                requirements={"r": ["blocked-controlled-restart"]},
            )
            resolver_started.wait()
            interrupt = client.client._start_session(action="interrupt")
            resolver_interrupted.wait()
            client.client._receive_many([controlled, interrupt])

            assert interrupt["result"] == {
                "content": [{"type": "text", "text": "[interrupt sent]"}],
                "isError": False,
            }, interrupt
            result = controlled["result"]
            assert result.get("isError") is True, result
            error = "".join(
                content["text"]
                for content in result["content"]
                if content["type"] == "text"
            )
            assert "R package resolution failed with exit status: 130" in error, error
            assert _tool_text(client.send()) == "\n[idle]"

            before_cleanup = client._read_open_capture(capture)
            assert not any(entry.keys() == {"server"} for entry in before_cleanup), (
                before_cleanup
            )
            transcript = client.finish_active()
            finished = True
        finally:
            if not resolver_released:
                resolver_release.release()
                resolver_released = True
            if not finished:
                stop_client(client.client)
                client._temporary.cleanup()
            capture.close()
            resolver_started.close()
            resolver_release.close()
            resolver_interrupted.close()

        assert not (root / "ir-counter").exists()

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [], commands
    return transcript


def test_standalone_interrupt_reaches_worker_during_controlled_interrupt(
    binary: Path,
) -> Transcript:
    client = ServerRelayClient(binary, "controlled_and_standalone_interrupts")
    client.send(
        r="old evaluation for overlapping interrupts",
        timeout_ms=0,
    )
    assert _tool_text(client.client.transcript[-1]["result"]) == (
        "\n[running; poll with an empty send]"
    )
    client._wait_for(EVALUATING_NAME)
    relay_root = client.relay_root()
    first_received = FifoCheckpoint(
        relay_root / CONTROLLED_INTERRUPT_FIRST_RECEIVED_NAME
    )
    second_received = FifoCheckpoint(
        relay_root / CONTROLLED_INTERRUPT_SECOND_RECEIVED_NAME
    )
    evaluation_release = FifoCheckpoint(
        relay_root / CONTROLLED_INTERRUPT_EVALUATION_RELEASE_NAME
    )
    released = False
    finished = False
    try:
        controlled = client.client._start_send(
            control="interrupt",
            r="new evaluation must not run while old evaluation is active",
        )
        first_received.wait()
        standalone = client.client._start_session(action="interrupt")
        second_received.wait()
        client.client._receive_many([controlled, standalone])

        controlled_result = controlled["result"]
        assert controlled_result.get("isError") is True, controlled_result
        controlled_text = "".join(
            content["text"]
            for content in controlled_result["content"]
            if content["type"] == "text"
        )
        assert "interrupted evaluation is still active" in controlled_text, (
            controlled_text
        )
        assert "cell was not run" in controlled_text, controlled_text
        assert standalone["result"] == {
            "content": [{"type": "text", "text": "[interrupt sent]"}],
            "isError": False,
        }, standalone

        evaluation_release.release()
        released = True
        assert _tool_text(client.send(timeout_ms=3_000)) == (
            "old evaluation finished after both interrupts\n"
        )
        transcript = client.finish_active()
        finished = True
    finally:
        if not released:
            evaluation_release.release()
        first_received.close()
        second_received.close()
        evaluation_release.close()
        if not finished:
            stop_client(client.client)
            client._temporary.cleanup()

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation for overlapping interrupts",
        },
        {"kind": "interrupt", "request_id": 0},
        {"kind": "interrupt", "request_id": 1},
    ], commands
    return transcript


def test_controlled_interrupt_does_not_run_cell_while_evaluation_remains_active(
    binary: Path,
) -> Transcript:
    client = ServerRelayClient(binary, "controlled_interrupt_still_active")
    client.send(r="old evaluation", timeout_ms=0)
    assert _tool_text(client.client.transcript[-1]["result"]) == (
        "\n[running; poll with an empty send]"
    )
    client._wait_for(EVALUATING_NAME)
    release = FifoCheckpoint(client.relay_root() / INTERRUPT_ACTIVE_RELEASE_NAME)
    finished = False
    released = False
    try:
        result = client.send(
            control="interrupt",
            r="new evaluation must not run",
            timeout_ms=1_000,
        )
        assert result.get("isError") is True, result
        text = "".join(
            content["text"]
            for content in result["content"]
            if content["type"] == "text"
        )
        assert "old evaluation remains active\n" in text, text
        assert "interrupted evaluation is still active" in text, text
        assert "cell was not run" in text, text

        release.release()
        released = True
        assert _tool_text(client.send(timeout_ms=3_000)) == (
            "old evaluation eventually finished\n"
        )
        transcript = client.finish_active()
        finished = True
    finally:
        if not released:
            release.release()
        release.close()
        if not finished:
            stop_client(client.client)
            client._temporary.cleanup()

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation",
        },
        {"kind": "interrupt", "request_id": 0},
    ], commands
    return transcript


def test_control_only_interrupt_honors_timeout_after_attachment(
    binary: Path,
) -> Transcript:
    client = ServerRelayClient(binary, "controlled_interrupt_still_active")
    client.send(r="old evaluation", timeout_ms=0)
    assert _tool_text(client.client.transcript[-1]["result"]) == (
        "\n[running; poll with an empty send]"
    )
    client._wait_for(EVALUATING_NAME)
    relay_root = client.relay_root()
    acknowledged = FifoCheckpoint(relay_root / INTERRUPT_ACKNOWLEDGED_NAME)
    release = FifoCheckpoint(relay_root / INTERRUPT_ACTIVE_RELEASE_NAME)
    finished = False
    released = False
    try:
        controlled = client.client._start_send(
            control="interrupt",
            timeout_ms=5_000,
        )
        acknowledged.wait()
        readable, _, _ = select.select([client.client.stdout], [], [], 0.25)
        assert not readable, "controlled interrupt ignored its attachment timeout"

        release.release()
        released = True
        client.client._receive(controlled)
        assert _tool_text(controlled["result"]) == (
            "old evaluation remains active\nold evaluation eventually finished\n"
        )
        transcript = client.finish_active()
        finished = True
    finally:
        if not released:
            release.release()
        acknowledged.close()
        release.close()
        if not finished:
            stop_client(client.client)
            client._temporary.cleanup()

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation",
        },
        {"kind": "interrupt", "request_id": 0},
    ], commands
    return transcript


def test_controlled_interrupt_does_not_wait_for_an_existing_poll(
    binary: Path,
) -> Transcript:
    client = ServerRelayClient(binary, "controlled_interrupt_with_waiting_poll")
    client.send(r="waiter-owned evaluation", timeout_ms=0)
    assert _tool_text(client.client.transcript[-1]["result"]) == (
        "\n[running; poll with an empty send]"
    )
    client._wait_for(EVALUATING_NAME)
    relay_root = client.relay_root()
    interrupt_received = FifoCheckpoint(relay_root / INTERRUPT_RECEIVED_NAME)
    interrupt_ack_release = FifoCheckpoint(relay_root / INTERRUPT_ACK_RELEASE_NAME)
    evaluation_release = FifoCheckpoint(relay_root / INTERRUPT_ACTIVE_RELEASE_NAME)
    interrupt_ack_released = False
    evaluation_released = False
    finished = False
    try:
        waiting = client.client._start_send(timeout_ms=5_000)
        # Filling the input pipe is a causal barrier: the ordered MCP input
        # transport must consume the preceding poll before this write returns.
        client.client._notify(
            "notifications/acceptance-test-barrier",
            padding="b" * (4 * 1024 * 1024),
        )
        controlled = client.client._start_send(
            control="interrupt",
            r="new evaluation must not run",
        )
        interrupt_received.wait()

        interrupt_ack_release.release()
        interrupt_ack_released = True
        readable, _, _ = select.select([client.client.stdout], [], [], 2)
        assert readable, "controlled interrupt waited for the existing poll timeout"
        client.client._receive(controlled)
        assert controlled["result"] == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "interrupted evaluation is still active; cell was not run"
                    ),
                }
            ],
            "isError": True,
        }, controlled
        assert "result" not in waiting, waiting

        evaluation_release.release()
        evaluation_released = True
        client.client._receive(waiting)
        assert _tool_text(waiting["result"]) == (
            "output owned by original waiter\noriginal waiter evaluation finished\n"
        )
        transcript = client.finish_active()
        finished = True
    finally:
        if not interrupt_ack_released:
            interrupt_ack_release.release()
        if not evaluation_released:
            evaluation_release.release()
        interrupt_received.close()
        interrupt_ack_release.close()
        evaluation_release.close()
        if not finished:
            stop_client(client.client)
            client._temporary.cleanup()

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {
            "kind": "evaluate",
            "language": "r",
            "source": "waiter-owned evaluation",
        },
        {"kind": "interrupt", "request_id": 0},
    ], commands
    return transcript


def test_orders_cross_source_output_by_serialized_observation(
    binary: Path,
) -> Transcript:
    client = ServerRelayClient(binary, "serialized_cross_source_order")
    evaluation = client.client._start_send(r="42")
    checkpoint = client._wait_for(CHECKPOINT_NAME)
    try:
        client.client._receive(evaluation)
        assert _tool_text(evaluation["result"]) == (
            "stdout before completion\n"
            "stderr before completion\n"
            "stdout after completion\n"
            "stderr after completion\n"
            "idle callback after completion\n"
        )
    finally:
        checkpoint.with_name(RELEASE_NAME).touch()
    client._wait_for(DONE_NAME)
    idle_output = _tool_text(client.send())
    assert idle_output == "stdout after grace\n\n[idle]", repr(idle_output)
    transcript = client.finish_active()

    completed = next(
        index
        for index, entry in enumerate(transcript)
        if entry == {"relay": {"kind": "completed"}}
    )
    first_callback = next(
        index
        for index, entry in enumerate(transcript)
        if entry.keys() == {"relay"} and entry["relay"].get("kind") == "resolve_r"
    )
    assert all(
        entry.keys() == {"relay"} for entry in transcript[completed:first_callback]
    )
    r_failures = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"}
        and entry["server"].get("kind") == "r_resolution_failed"
    ]
    assert r_failures == [
        {
            "kind": "r_resolution_failed",
            "failure": "host",
            "message": (
                "automatic R package name `github::owner/repo` is not accepted: "
                "names must start with an ASCII letter, end with an ASCII letter "
                "or digit, and contain only ASCII letters, digits, and dots"
            ),
        }
    ]
    return transcript


def test_gracefully_shuts_down(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "shutdown")
    assert _tool_text(client.session(action="restart")) == (
        "[starting new worker]\n[idle]"
    )
    return client.finish_shutdown()


def test_shutdown_precedes_blocked_resolver_cancellation(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "blocked-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        resolver_started = FifoCheckpoint(root / "resolver-started", create=True)
        resolver_release = root / "resolver-release"
        os.mkfifo(resolver_release)
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release)

        client = ServerRelayClient(
            binary,
            "blocked_live_r_resolver_shutdown",
            environment,
        )
        client.start_worker()
        relay_root = client.relay_root()
        capture = (relay_root / CAPTURE_NAME).open(encoding="utf-8")
        shutdown_received = FifoCheckpoint(relay_root / SHUTDOWN_RECEIVED_NAME)
        retirement_release = FifoCheckpoint(relay_root / RETIREMENT_RELEASE_NAME)
        finished = False
        try:
            preparation = client.client._start_send(
                r="must not execute during shutdown",
                requirements={"r": ["blocked-resolver"]},
            )
            resolver_started.wait()
            client.client.stdin.close()
            shutdown_received.wait()
            # Shutdown receipt precedes resolver cancellation. This response
            # proves the cancelled resolver callback has now returned.
            _receive_checkpointed(
                client.client,
                preparation,
                "the cancelled R preparation",
            )
            result = preparation["result"]
            assert result.get("isError") is True, result
            assert result["content"] == [
                {"type": "text", "text": "R package resolution cancelled"}
            ], result
            retirement_release.release()
            client.client._finish()
            finished = True
            transcript = client._read_open_capture(capture)
        finally:
            if not finished:
                stop_client(client.client)
            capture.close()
            shutdown_received.close()
            retirement_release.close()
            resolver_started.close()
            client._temporary.cleanup()

    shutdown = _normalize_shutdown_grace(transcript)
    assert len(shutdown) == 1, transcript
    server_commands = [
        entry["server"] for entry in transcript if entry.keys() == {"server"}
    ]
    assert server_commands == shutdown, server_commands
    return transcript


def test_cancelled_send_returns_owned_output_to_restart(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "cancelled_waiting_send")
    client.start_worker()
    relay_root = client.relay_root()
    prelude_release = FifoCheckpoint(relay_root / PRELUDE_RELEASE_NAME)
    prelude_processed = FifoCheckpoint(relay_root / PRELUDE_PROCESSED_NAME)
    output_ready = FifoCheckpoint(relay_root / EVALUATION_OUTPUT_READY_NAME)
    shutdown_received = FifoCheckpoint(relay_root / SHUTDOWN_RECEIVED_NAME)
    retirement_release = FifoCheckpoint(relay_root / RETIREMENT_RELEASE_NAME)
    finished = False
    retirement_released = False
    try:
        prelude_release.release()
        prelude_processed.wait()

        waiting = client.client._start_send(r="42", timeout_ms=30_000)
        output_ready.wait()
        restart = client.client._start_session(action="restart")
        shutdown_received.wait()
        client.client._notify(
            "notifications/cancelled",
            requestId=waiting["id"],
            reason="acceptance test cancelled the waiting send",
        )
        cancellation = client.client.transcript[-1]["input"]["params"]
        assert cancellation["requestId"] == waiting["id"], cancellation
        cancellation["requestId"] = "<request ID>"
        retirement_release.release()
        retirement_released = True
        client.client._receive(restart)

        assert "result" not in waiting, waiting
        result = restart["result"]
        assert result["isError"] is True, result
        assert [content["type"] for content in result["content"]] == [
            "text",
            "image",
            "text",
            "image",
            "text",
        ], result
        assert result["content"][0]["text"] == "idle before image\n", result
        assert result["content"][1] == {
            "type": "image",
            "data": PNG_1X1,
            "mimeType": "image/png",
        }, result
        assert result["content"][2]["text"] == (
            "idle after image\n[output produced while idle]\ncell before image\n"
        ), result
        assert result["content"][3] == {
            "type": "image",
            "data": PNG_1X1,
            "mimeType": "image/png",
        }, result

        cell_prefix = "cell before image\n"
        retained = "x" * (PENDING_TEXT_BUDGET - len(cell_prefix))
        omitted = len(cell_prefix) + 7
        truncation = (
            f"[output truncated: omitted {omitted} text bytes and "
            "0 encoded image bytes across 1 event]"
        )
        tail = result["content"][4]["text"]
        assert tail.startswith(retained + "\n" + truncation), (
            f"unexpected reclaimed tail: length={len(tail)}, tail={tail[-500:]!r}"
        )
        for notice in (
            truncation,
            "[stopped by session restart request before evaluation finished]",
            "[worker stopped: in-memory state lost]",
            "[active evaluation stopped by session restart request]",
            "[starting new worker]",
            "[idle]",
        ):
            assert tail.count(notice) == 1, (notice, tail[-1_000:])
        result["content"][4]["text"] = tail.replace(
            retained,
            f"<retained {len(retained)} text bytes>",
            1,
        )

        client.send()
        assert _tool_text(client.client.transcript[-1]["result"]) == "\n[idle]"
        transcript = client.client._finish()
        finished = True
        return transcript
    finally:
        if not retirement_released:
            retirement_release.release()
        if not finished:
            stop_client(client.client)
        prelude_release.close()
        prelude_processed.close()
        output_ready.close()
        shutdown_received.close()
        retirement_release.close()
        client._temporary.cleanup()


def test_restart_consumes_late_r_preparation_retirement_events(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        libraries = [root / "candidate-one", root / "candidate-two"]
        for library in libraries:
            library.mkdir()
        environment = _fake_ir_environment(root, libraries)
        client = ServerRelayClient(
            binary,
            "late_r_prepared_retirement",
            environment,
        )
        client.start_worker()
        old_root = client.relay_root()
        old_capture = (old_root / CAPTURE_NAME).open(encoding="utf-8")
        preparation_received = FifoCheckpoint(old_root / PREPARATION_RECEIVED_NAME)
        shutdown_received = FifoCheckpoint(old_root / SHUTDOWN_RECEIVED_NAME)
        retirement_release = FifoCheckpoint(old_root / RETIREMENT_RELEASE_NAME)
        finished = False
        try:
            preparation = client.client._start_send(
                r="must not execute after resolver restart race",
                requirements={"r": ["ordered-retirement"]},
            )
            preparation_received.wait()
            restart = client.client._start_session(action="restart")
            shutdown_received.wait()
            # The preparation response is released by the ordered retirement
            # marker, so the old relay result below is necessarily late.
            _receive_checkpointed(
                client.client,
                preparation,
                "the retired R preparation",
            )
            preparation_result = preparation["result"]
            assert preparation_result == {
                "content": [
                    {"type": "text", "text": "R preparation cancelled by restart"}
                ],
                "isError": True,
            }, preparation_result
            retirement_release.release()
            _receive_checkpointed(client.client, restart, "restart")
            restart_result = restart["result"]
            assert restart_result.get("isError") is not True, restart_result
            output = restart_result["content"][0]["text"]
            assert output == (
                "drained old stdout\n"
                "drained old stderr\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            ), output
            assert "status 33" not in output, output

            old_transcript = client._read_open_capture(old_capture)
            replacement_root = client.relay_root()
            assert replacement_root != old_root
            replacement_capture = (replacement_root / CAPTURE_NAME).open(
                encoding="utf-8"
            )
            try:
                assert (
                    _tool_text(
                        client.session(
                            action="prepare",
                            requirements={"r": ["ordered-retirement"]},
                        )
                    )
                    == "[prepared]"
                )
                client.client._finish()
                finished = True
                replacement_transcript = client._read_open_capture(replacement_capture)
            finally:
                replacement_capture.close()
        finally:
            if not finished:
                stop_client(client.client)
            old_capture.close()
            preparation_received.close()
            shutdown_received.close()
            retirement_release.close()
            client._temporary.cleanup()

    transcript = old_transcript + replacement_transcript
    shutdown = _normalize_shutdown_grace(transcript)
    assert len(shutdown) == 2, transcript
    prepare_commands = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "prepare_r"
    ]
    assert [command["library"] for command in prepare_commands] == list(
        map(str, libraries)
    ), prepare_commands
    for command in prepare_commands:
        command["library"] = f"<{Path(command['library']).name}>"
    prepared_events = [
        entry["relay"]
        for entry in transcript
        if entry.keys() == {"relay"} and entry["relay"].get("kind") == "r_prepared"
    ]
    assert [event["library"] for event in prepared_events] == list(map(str, libraries))
    for event in prepared_events:
        event["library"] = f"<{Path(event['library']).name}>"
    assert not any(
        entry.keys() == {"server"} and entry["server"].get("kind") == "evaluate"
        for entry in transcript
    ), transcript
    return transcript


def test_restart_discards_pre_marker_r_preparation_result(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        libraries = [
            root / "candidate-one",
            root / "candidate-two",
            root / "candidate-three",
        ]
        for library in libraries:
            library.mkdir()
        environment = _fake_ir_environment(root, libraries)
        resolver_started = FifoCheckpoint(root / "resolver-started", create=True)
        resolver_release = FifoCheckpoint(root / "resolver-release", create=True)
        environment["MCP_CONSOLE_TEST_IR_GATE_INDEX"] = "1"
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)

        client = ServerRelayClient(
            binary,
            "pre_marker_r_prepared_replacement",
            environment,
        )
        client.start_worker()
        old_root = client.relay_root()
        old_capture = (old_root / CAPTURE_NAME).open(encoding="utf-8")
        preparation_received = FifoCheckpoint(old_root / PREPARATION_RECEIVED_NAME)
        result_release = FifoCheckpoint(old_root / PREPARATION_RESULT_RELEASE_NAME)
        result_sent = FifoCheckpoint(old_root / PREPARATION_RESULT_SENT_NAME)
        shutdown_received = FifoCheckpoint(old_root / SHUTDOWN_RECEIVED_NAME)
        finished = False
        try:
            preparation = client.client._start_session(
                action="prepare",
                requirements={"r": ["old-generation"]},
            )
            preparation_received.wait()
            restart = client.client._start_session(
                action="restart",
                requirements={"r": ["replacement-generation"]},
            )
            resolver_started.wait()
            result_release.release()
            result_sent.wait()
            resolver_release.release()
            client.client._receive_many([preparation, restart])

            assert preparation["result"] == {
                "content": [
                    {"type": "text", "text": "R preparation cancelled by restart"}
                ],
                "isError": True,
            }, preparation
            restart_result = restart["result"]
            assert restart_result.get("isError") is not True, restart_result
            assert restart_result["content"] == [
                {
                    "type": "text",
                    "text": (
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                }
            ], restart_result
            shutdown_received.wait()

            old_transcript = client._read_open_capture(old_capture)
            replacement_root = client.relay_root()
            assert replacement_root != old_root
            replacement_capture = (replacement_root / CAPTURE_NAME).open(
                encoding="utf-8"
            )
            try:
                assert (
                    _tool_text(
                        client.session(
                            action="prepare",
                            requirements={"r": ["replacement-generation"]},
                        )
                    )
                    == "[prepared]"
                )
                # The old result did not enter the replacement requirement set.
                assert (
                    _tool_text(
                        client.session(
                            action="prepare",
                            requirements={"r": ["old-generation"]},
                        )
                    )
                    == "[prepared]"
                )
                client.client._finish()
                finished = True
                replacement_transcript = client._read_open_capture(replacement_capture)
            finally:
                replacement_capture.close()
        finally:
            if not finished:
                stop_client(client.client)
            old_capture.close()
            preparation_received.close()
            result_release.close()
            result_sent.close()
            shutdown_received.close()
            resolver_started.close()
            resolver_release.close()
            client._temporary.cleanup()

    transcript = old_transcript + replacement_transcript
    shutdown = _normalize_shutdown_grace(transcript)
    assert len(shutdown) == 2, transcript
    prepare_commands = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "prepare_r"
    ]
    assert [command["library"] for command in prepare_commands] == [
        str(libraries[0]),
        str(libraries[2]),
    ], prepare_commands
    for command in prepare_commands:
        command["library"] = f"<{Path(command['library']).name}>"
    prepared_events = [
        entry["relay"]
        for entry in transcript
        if entry.keys() == {"relay"} and entry["relay"].get("kind") == "r_prepared"
    ]
    assert [event["library"] for event in prepared_events] == [
        str(libraries[0]),
        str(libraries[2]),
    ], prepared_events
    for event in prepared_events:
        event["library"] = f"<{Path(event['library']).name}>"
    return transcript


def test_r_preparation_failure_requires_restart_and_preserves_worker(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "failed-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        client = ServerRelayClient(
            binary,
            "r_preparation_failure",
            environment,
        )
        client.start_worker()

        result = client.send(
            r="must not execute",
            requirements={"r": ["failing-preparation"]},
        )
        assert result == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "scripted R preparation failed; further requirement "
                        "changes are unavailable until session restart"
                    ),
                }
            ],
            "isError": True,
        }, result

        assert _tool_text(client.send(r="42")) == "[done]"
        result = client.send(
            r="must not execute after restart requirement",
            requirements={"r": ["not-forwarded"]},
        )
        assert result == {
            "content": [
                {
                    "type": "text",
                    "text": ("requirements require session restart; cell was not run"),
                }
            ],
            "isError": True,
        }, result
        transcript = client.finish_active()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    prepare_commands = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "prepare_r"
    ]
    assert prepare_commands == [{"kind": "prepare_r", "library": str(library)}]
    prepare_commands[0]["library"] = "<failed-candidate>"
    evaluations = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "evaluate"
    ]
    assert evaluations == [{"kind": "evaluate", "language": "r", "source": "42"}]
    return transcript


def test_rejects_runtime_r_resolution_during_r_preparation(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        libraries = [root / "explicit-candidate", root / "nested-candidate"]
        for library in libraries:
            library.mkdir()
        environment = _fake_ir_environment(root, libraries)
        client = ServerRelayClient(
            binary,
            "r_resolution_during_r_preparation",
            environment,
        )
        client.start_worker()
        old_root = client.relay_root()
        checkpoint = FifoCheckpoint(old_root / R_PREPARATION_RESOLVE_CHECKPOINT_NAME)
        release = FifoCheckpoint(old_root / R_PREPARATION_RESOLVE_RELEASE_NAME)
        old_capture = (old_root / CAPTURE_NAME).open(encoding="utf-8")
        finished = False
        try:
            evaluation = client.client._start_send(
                r="must not execute",
                requirements={"r": ["explicit-package"]},
            )
            checkpoint.wait()
            release.release()
            _receive_checkpointed(client.client, evaluation, "the rejected R callback")
            result = evaluation["result"]
            assert result.get("isError") is True, result
            output = result["content"][0]["text"]
            assert output.endswith("[worker stopped: in-memory state lost]"), output
            old_transcript = client._read_open_capture(old_capture)
            client.client._finish()
            finished = True
        finally:
            if not finished:
                stop_client(client.client)
            old_capture.close()
            checkpoint.close()
            release.close()
            client._temporary.cleanup()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    transcript = old_transcript
    shutdown = _normalize_shutdown_grace(transcript)
    assert len(shutdown) == 1, transcript
    prepare_commands = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "prepare_r"
    ]
    assert prepare_commands == [{"kind": "prepare_r", "library": str(libraries[0])}]
    prepare_commands[0]["library"] = "<explicit-candidate>"
    assert not any(
        entry.keys() == {"server"}
        and entry["server"].get("kind") in {"r_resolved", "r_resolution_failed"}
        for entry in transcript
    ), transcript
    assert not any(
        entry.keys() == {"server"}
        and entry["server"].get("kind") == "evaluate"
        and entry["server"].get("source") == "must not execute"
        for entry in transcript
    ), transcript
    return transcript


def test_idle_runtime_r_resolution_owns_environment_until_activation(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        libraries = [root / "automatic-candidate", root / "stale-explicit-candidate"]
        for library in libraries:
            library.mkdir()
        environment = _fake_ir_environment(root, libraries)
        resolver_started = FifoCheckpoint(root / "resolver-started", create=True)
        resolver_release = FifoCheckpoint(root / "resolver-release", create=True)
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        client = ServerRelayClient(
            binary,
            "idle_r_resolution_owns_environment",
            environment,
        )
        client.start_worker()
        relay_root = client.relay_root()
        ready = FifoCheckpoint(relay_root / IDLE_R_RESOLUTION_READY_NAME)
        release = FifoCheckpoint(relay_root / IDLE_R_RESOLUTION_RELEASE_NAME)
        evaluation_received = FifoCheckpoint(
            relay_root / IDLE_R_EVALUATION_RECEIVED_NAME
        )
        resolver_released = False
        ready_reached = False
        activation_released = False
        finished = False
        try:
            resolver_started.wait()
            preparation = client.client._start_session(
                action="prepare",
                requirements={"r": ["english"]},
            )
            _receive_checkpointed(
                client.client,
                preparation,
                "explicit preparation while the idle R resolver was blocked",
            )
            _tool_error(preparation, "idle runtime R callback owns environment changes")

            assert (
                _tool_text(client.send(r="42", timeout_ms=0))
                == "\n[running; poll with an empty send]"
            )
            resolver_release.release()
            resolver_released = True
            ready.wait()
            ready_reached = True
            evaluation_received.wait()
            release.release()
            activation_released = True
            assert _tool_text(client.send()) == "[done]"
            transcript = client.finish_active()
            finished = True
        finally:
            if not resolver_released:
                resolver_release.release()
            if not ready_reached:
                ready.wait()
            if not activation_released:
                release.release()
            resolver_started.close()
            resolver_release.close()
            ready.close()
            release.close()
            evaluation_received.close()
            if not finished:
                stop_client(client.client)
                client._temporary.cleanup()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    assert not any(
        entry.keys() == {"server"}
        and entry["server"].get("kind") in {"prepare_r", "r_resolution_failed"}
        for entry in transcript
    ), transcript
    for entry in transcript:
        message = entry.get("server", entry.get("relay", {}))
        if message.get("kind") in {"r_resolved", "r_activated"}:
            assert message["library"] == str(libraries[0]), message
            message["library"] = "<automatic-candidate>"
    return transcript


def test_explicit_r_preparation_owns_environment_before_host_resolution(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "explicit-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        resolver_started = FifoCheckpoint(root / "resolver-started", create=True)
        resolver_release = FifoCheckpoint(root / "resolver-release", create=True)
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        client = ServerRelayClient(
            binary,
            "explicit_r_preparation_owns_environment",
            environment,
        )
        client.start_worker()
        relay_root = client.relay_root()
        callback = FifoCheckpoint(relay_root / EXPLICIT_R_PREPARATION_CALLBACK_NAME)
        callback_reply = FifoCheckpoint(
            relay_root / EXPLICIT_R_PREPARATION_CALLBACK_REPLY_NAME
        )
        callback_released = False
        resolver_released = False
        finished = False
        try:
            preparation = client.client._start_session(
                action="prepare",
                requirements={"r": ["english"]},
            )
            resolver_started.wait()
            callback.release()
            callback_released = True
            callback_reply.wait()
            readable, _, _ = select.select([client.client.stdout], [], [], 0.25)
            assert not readable, (
                "preparation completed before its resolver was released"
            )

            resolver_release.release()
            resolver_released = True
            client.client._receive(preparation)
            assert _tool_text(preparation["result"]) == "[prepared]"
            assert _tool_text(client.send(r="42")) == "[done]"
            transcript = client.finish_active()
            finished = True
        finally:
            if not callback_released:
                callback.release()
            if not resolver_released:
                resolver_release.release()
            callback.close()
            callback_reply.close()
            resolver_started.close()
            resolver_release.close()
            if not finished:
                stop_client(client.client)
                client._temporary.cleanup()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    failures = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"}
        and entry["server"].get("kind") == "r_resolution_failed"
    ]
    assert failures == [
        {
            "kind": "r_resolution_failed",
            "failure": "host",
            "message": (
                "R package resolution is unavailable during requirement preparation"
            ),
        }
    ]
    prepare_commands = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "prepare_r"
    ]
    assert prepare_commands == [{"kind": "prepare_r", "library": str(library)}]
    prepare_commands[0]["library"] = "<explicit-candidate>"
    prepared = [
        entry["relay"]
        for entry in transcript
        if entry.keys() == {"relay"} and entry["relay"].get("kind") == "r_prepared"
    ]
    assert prepared == [{"kind": "r_prepared", "library": str(library)}]
    prepared[0]["library"] = "<explicit-candidate>"
    runtime_environment = [
        message
        for entry in transcript
        if entry.keys() in ({"server"}, {"relay"})
        and (message := next(iter(entry.values()))).get("kind")
        in {"r_resolved", "r_activated"}
    ]
    assert runtime_environment == [
        {"kind": "r_resolved", "library": str(library)},
        {"kind": "r_activated", "library": str(library)},
    ]
    for message in runtime_environment:
        message["library"] = "<explicit-candidate>"
    return transcript


def test_rejects_completion_before_runtime_r_activation(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "automatic-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        client = ServerRelayClient(
            binary,
            "completion_before_r_activation",
            environment,
        )
        client.start_worker()
        old_root = client.relay_root()
        old_capture = (old_root / CAPTURE_NAME).open(encoding="utf-8")
        finished = False
        try:
            result = client.send(r="42")
            assert result.get("isError") is True, result
            output = result["content"][0]["text"]
            assert output.startswith(
                "[worker sent an operation result before completing runtime R "
                "activation]\n"
            ), output
            assert output.endswith(
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            ), output
            old_transcript = client._read_open_capture(old_capture)
            client.client._finish()
            finished = True
        finally:
            if not finished:
                stop_client(client.client)
            old_capture.close()
            client._temporary.cleanup()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    transcript = old_transcript
    shutdown = _normalize_shutdown_grace(transcript)
    assert len(shutdown) == 1, transcript
    resolved = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "r_resolved"
    ]
    assert resolved == [{"kind": "r_resolved", "library": str(library)}]
    resolved[0]["library"] = "<automatic-candidate>"
    assert not any(
        entry.keys() == {"relay"}
        and entry["relay"].get("kind") in {"r_activated", "r_activation_failed"}
        for entry in transcript
    ), transcript
    return transcript


def test_reports_fatal_failure(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "fatal")
    failed = client.client._start_send(r="42")
    transcript = client.release_failure(failed, "scripted relay failure")
    output = failed["result"]["content"][0]["text"]
    assert output.startswith("drained after fatal failure\n"), output
    assert "[worker exited with status 86]" in output, output
    assert transcript[-1] == {"relay": {"kind": "worker_exited", "code": 86}}, (
        transcript
    )
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
    assert raw == b'{"kind":"console_output"', raw
    return transcript


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


def test_reports_unexpected_worker_exit_zero(binary: Path) -> Transcript:
    transcript, _ = _reports_worker_outcome(
        binary,
        "exit_zero",
        "[worker exited with status 0]",
    )
    assert transcript[-1] == {"relay": {"kind": "worker_exited", "code": 0}}, transcript
    return transcript


def test_reports_unexpected_worker_exit_nonzero_and_drains_output(
    binary: Path,
) -> Transcript:
    transcript, output = _reports_worker_outcome(
        binary,
        "exit_nonzero",
        "[worker exited with status 33]",
    )
    assert output.startswith("drained stdout\n�drained stderr\n"), output
    events = [entry["relay"] for entry in transcript if entry.keys() == {"relay"}]
    assert events[-6:] == [
        {"kind": "stdout", "data": "drained stdout\n"},
        {
            "kind": "stderr_bytes",
            "data": base64.b64encode(b"\xffdrained stderr\n").decode("ascii"),
        },
        {"kind": "stdout_closed"},
        {"kind": "stderr_closed"},
        {"kind": "worker_sideband_closed"},
        {"kind": "worker_exited", "code": 33},
    ], events
    return transcript


def test_reports_unexpected_worker_signal(binary: Path) -> Transcript:
    transcript, _ = _reports_worker_outcome(
        binary,
        "signaled",
        "[worker terminated by signal 15]",
    )
    assert transcript[-1] == {"relay": {"kind": "worker_signaled", "signal": 15}}, (
        transcript
    )
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
