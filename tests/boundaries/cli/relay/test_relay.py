#!/usr/bin/env -S uv run --script

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.capture import read_lines
from support.checkpoints import FifoCheckpoint
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.processes import (
    capture_process_identity,
    kill_processes,
    live_processes,
)
from support.native import SHARED_LIBRARY_FLAG
from support.native import LOADER_VARIABLE
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, WORKER, requires
from support.suites import run_this_suite


@requires(WORKER, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_retires_while_descendant_keeps_refilling_output(binary: Path) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    transcript: Transcript = []
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        interposer = temporary / "relay-refill.dylib"
        subprocess.run(
            [
                "cc",
                SHARED_LIBRARY_FLAG,
                "-fPIC",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                interposer,
                fixtures / "native" / "relay_refill_interposer.c",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        for stream in ("stdout", "stderr", "sideband"):
            root = temporary / stream
            root.mkdir()
            checkpoints = {
                name: FifoCheckpoint.create(root / name)
                for name in (
                    "refill-request",
                    "refill-done",
                    "refill-observed",
                    "worker-exit",
                )
            }
            environment = os.environ.copy()
            environment["TMPDIR"] = str(root)
            environment[LOADER_VARIABLE] = str(interposer)
            environment["MCP_CONSOLE_TEST_REFILL_MATCH"] = (
                '{"kind":"console_output","data":"descendant output\\n"}\n'
                if stream == "sideband"
                else "descendant output\n"
            )
            for name in ("request", "done", "observed"):
                environment[f"MCP_CONSOLE_TEST_REFILL_{name.upper()}"] = str(
                    root / f"refill-{name}"
                )
            process = subprocess.Popen(
                [
                    binary,
                    "worker-relay",
                    sys.executable,
                    fixtures / "relay_worker" / "refilling_descendant.py",
                    stream,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                start_new_session=True,
            )
            descendant = None
            try:
                assert process.stdin is not None and process.stdout is not None
                ready = read_lines(process.stdout, 1, "relay ready")
                assert [json.loads(line) for line in ready] == [{"kind": "ready"}]
                process.stdin.write(
                    json.dumps({"kind": "stdin", "data": "start\n"}) + "\n"
                )
                process.stdin.flush()
                checkpoints["refill-observed"].wait(
                    "descendant output was read and refilled"
                )
                descendant = capture_process_identity(
                    int((root / "descendant-pid").read_text())
                )
                checkpoints["worker-exit"].release()
                # communicate drains output while enforcing a failure deadline.
                # Keep command input live: natural worker exit triggers retirement.
                command_input = process.stdin
                process.stdin = None
                try:
                    stdout, stderr = process.communicate(timeout=5)
                finally:
                    command_input.close()
                assert process.returncode == 0, stderr
                assert stderr == "", stderr
                events = [json.loads(line) for line in stdout.splitlines()]
                output_kind = "console_output" if stream == "sideband" else stream
                outputs = [event for event in events if event["kind"] == output_kind]
                assert outputs, events
                assert all(
                    event == {"kind": output_kind, "data": "descendant output\n"}
                    for event in outputs
                ), outputs
                terminal_events = [
                    event for event in events if event["kind"] != output_kind
                ]
                assert terminal_events == [
                    {"kind": "stdout_closed"},
                    {"kind": "stderr_closed"},
                    {"kind": "worker_sideband_closed"},
                    {"kind": "worker_exited", "code": 0},
                ], terminal_events
                assert events == outputs + terminal_events, events
                assert live_processes([descendant]) == [descendant[0]]
                # The deadline makes the repetition count incidental. Retain
                # its exact payload and the complete terminal protocol suffix.
                transcript.append(
                    {
                        "refilled_stream": stream,
                        "repeated_output": outputs[0],
                        "events": terminal_events,
                    }
                )
            finally:
                # Before descendant capture, no wait has reaped the group
                # leader. Retire that group before poll can release its PID.
                if descendant is None or process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if descendant is not None:
                    kill_processes([descendant])
                process.communicate(timeout=10)
                for checkpoint in checkpoints.values():
                    checkpoint.close()
    return transcript


@executions(DIRECT, SANDBOXED)
def test_relay_protocol_is_independent_of_sandbox_launch(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    transcript: Transcript = []
    with tempfile.TemporaryDirectory() as directory:
        environment = os.environ.copy()
        environment["TMPDIR"] = directory
        command = execution.command(binary, "worker-relay", sys.executable, str(worker))
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            if execution == DIRECT:
                assert os.getpgid(process.pid) == os.getpgrp()
            assert process.stdin is not None and process.stdout is not None
            events = [
                json.loads(line) for line in read_lines(process.stdout, 1, "ready")
            ]
            assert events == [{"kind": "ready"}], events
            evaluate = {"kind": "evaluate", "language": "r", "source": "echo hello"}
            process.stdin.write(json.dumps(evaluate) + "\n")
            process.stdin.flush()
            events.extend(
                json.loads(line)
                for line in read_lines(process.stdout, 3, "evaluation completion")
            )
            shutdown = {"kind": "shutdown", "grace_millis": 1000}
            stdout, stderr = process.communicate(
                json.dumps(shutdown) + "\n", timeout=10
            )
            events.extend(json.loads(line) for line in stdout.splitlines())
            assert process.returncode == 0, stderr
            assert stderr == "", stderr
            assert events == [
                {"kind": "ready"},
                {"kind": "console_output", "data": "zod: "},
                {"kind": "console_output", "data": "hello\n"},
                {"kind": "completed"},
                {"kind": "shutdown_started"},
                {"kind": "stdout_closed"},
                {"kind": "stderr_closed"},
                {"kind": "worker_sideband_closed"},
                {"kind": "worker_exited", "code": 0},
            ], events
            transcript.append({"events": events})
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
