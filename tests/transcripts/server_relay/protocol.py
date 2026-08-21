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
SHUTDOWN_RECEIVED_NAME = "mcp-console-scripted-relay-shutdown-received"
RETIREMENT_RELEASE_NAME = "mcp-console-scripted-relay-retirement-release"
PREPARATION_RECEIVED_NAME = "mcp-console-scripted-relay-preparation-received"


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
            assert allow_raw and entry.keys() == {"relay_raw"}, entry
            base64.b64decode(entry["relay_raw"], validate=True)
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


def test_orders_cross_source_output_by_serialized_observation(
    binary: Path,
) -> Transcript:
    client = ServerRelayClient(binary, "serialized_cross_source_order")
    assert _tool_text(client.send(r="42")) == "[done]"
    client._wait_for(DONE_NAME)
    assert _tool_text(client.send()) == "observed after operation result\n[idle]"
    return client.finish_active()


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
            preparation = client.client._start_session(
                action="prepare",
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
            preparation = client.client._start_session(
                action="prepare",
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
