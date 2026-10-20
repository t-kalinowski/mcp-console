#!/usr/bin/env -S uv run --script

import json
import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.recording.test_process import recording_events
from support.allocations import RetentionProfile
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, requires
from support.suites import run_this_suite


@requires(NATIVE_FIXTURES)
def test_protocol_writer_does_not_hold_frame_reception(binary: Path) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with (
            closing(FifoCheckpoint.create(root / "observed")) as observed,
            closing(FifoCheckpoint.create(root / "release")) as release,
        ):
            environment = {
                **os.environ,
                LOADER_VARIABLE: str(build_interposer(root, "recording_observation")),
                "MCP_CONSOLE_TEST_OBSERVED_EVENT": "protocol",
                "MCP_CONSOLE_TEST_JOURNAL_OBSERVED": str(observed.path),
                "MCP_CONSOLE_TEST_JOURNAL_RELEASE": str(release.path),
            }
            with McpClient(
                binary,
                DIRECT.serve("--worker", str(worker)),
                environment,
                root,
                response_timeout=5,
            ) as client:
                try:
                    client.initialize_and_list_tools()
                    observed.wait("protocol journal writer stalled", client=client)
                    pending = client.start_send()
                    client.notify("notifications/cancelled", requestId=pending["id"])
                    assert client.request("ping")["result"] == {}
                    client.stdin.close()
                finally:
                    release.release()
                transcript = client.finish()
            (recording,) = (root / ".agents/console/sessions").iterdir()
            events = recording_events(recording)
            assert [e["request_id"] for e in events if e["event"] == "tool_call"] == [
                pending["id"]
            ]
            received = [
                e["message"]
                for e in events
                if e["event"] == "protocol" and e["direction"] == "received"
            ]
            assert [e["method"] for e in received][-3:] == [
                "tools/call",
                "notifications/cancelled",
                "ping",
            ]
            assert events[-1]["event"] == "recording_ended"
            return transcript


@requires(POSIX)
def test_restart_failures_only_number_acquired_workers(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        worker = root / "worker"
        source = f'#!/bin/sh\nexec "{zod}"\n'
        worker.write_text(source)
        worker.chmod(0o700)
        control = root / "startup-control"
        control.write_text("ready")
        with McpClient(
            binary,
            DIRECT.serve("--worker", str(worker)),
            {**os.environ, "ZOD_STARTUP_CONTROL": str(control)},
            root,
        ) as client:
            client.initialize_and_list_tools()
            assert not client.send(r="echo ready")["isError"]
            control.write_text("fail with stderr")
            assert client.send(control="restart")["isError"]
            worker.unlink()
            assert client.send(control="restart")["isError"]
            worker.write_text(source)
            worker.chmod(0o700)
            control.write_text("ready")
            assert not client.send(control="restart")["isError"]
            assert not client.send(r="echo recovered")["isError"]
            client.finish()
        (recording,) = (root / ".agents/console/sessions").iterdir()
        events = recording_events(recording)
        assert [e["session_id"] for e in events if e["event"] == "worker_started"] == [
            1,
            2,
            3,
        ]
        assert [e["session_id"] for e in events if e["event"] == "worker_ready"] == [
            1,
            3,
        ]
        assert [e["session_id"] for e in events if e["event"] == "worker_ended"] == [
            1,
            2,
            3,
        ]
        failures = [e for e in events if e["event"] == "startup_failed"]
        assert [e["session_id"] for e in failures] == [2, None]
        assert (
            failures[0]["message"]
            == "worker sideband read failed: worker sideband closed"
        )
        assert (
            next(
                e["outcome"]
                for e in events
                if e["event"] == "worker_ended" and e["session_id"] == 2
            )
            == "exited with code 86"
        )
        assert "failed to launch worker" in failures[1]["message"]
        # Full platform diagnostics remain in existing launch-failure transcripts.
        return [
            *client.transcript[:3],
            {
                "worker sessions": [1, 2, 3],
                "readiness": [1, 3],
                "startup failures": [2, None],
            },
        ]


def first_send_without_code(binary: Path, arguments: dict) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with McpClient(
            binary, DIRECT.serve("--worker", str(worker)), current_directory=root
        ) as client:
            client.initialize_and_list_tools()
            assert not (root / ".agents/console/sessions").exists()
            client.send(**arguments)
            transcript = client.finish()
        (recording,) = (root / ".agents/console/sessions").iterdir()
        events = recording_events(recording)
        assert [
            e["request"]["arguments"] for e in events if e["event"] == "tool_call"
        ] == [arguments]
        assert not any(e["event"] == "cell_output" for e in events)
        assert events[-1]["event"] == "recording_ended"
        return transcript


@requires(POSIX)
def test_first_poll_activates_recording(binary: Path) -> Transcript:
    return first_send_without_code(binary, {})


@requires(POSIX)
def test_first_input_activates_recording(binary: Path) -> Transcript:
    return first_send_without_code(binary, {"stdin": "unused input"})


@requires(POSIX)
def test_first_control_activates_recording(binary: Path) -> Transcript:
    return first_send_without_code(binary, {"control": "restart"})


@requires(NATIVE_FIXTURES)
@executions(DIRECT)
def test_releases_completed_stdin_requests(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with closing(RetentionProfile(workspace)) as profile:
            environment = {**os.environ, **profile.environment}
            with McpClient(
                binary, execution.serve("--worker", str(worker)), environment, workspace
            ) as client:
                client.initialize_and_list_tools()
                client.send(r="echo ready")
                retained = []
                for _ in range(2):
                    for _ in range(24):
                        client.send(stdin="x" * (1024 * 1024))
                    client.send(control="restart")
                    assert client.request("ping")["result"] == {}
                    retained.append(profile.live)
                # Permit live transport copies, but not another batch of retained input.
                assert retained[1] - retained[0] < 4 * 1024 * 1024, retained
                client.finish()
                return [
                    *client.transcript[:3],
                    {"request memory": "released across batches and restart"},
                ]


if __name__ == "__main__":
    run_this_suite(__file__)
