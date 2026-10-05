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
from support.events import Events
from support.native import LOADER_VARIABLE, build_interposer
from support.processes import (
    capture_process_identity,
    live_processes,
    stop_process_group,
)
from support.records import Transcript
from support.relay_lifecycle import (
    LIFECYCLE_COMMANDS,
    assert_exit_tail,
    assert_failure_tail,
)
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, WORKER, requires
from support.suites import run_this_suite


@requires(WORKER, PROCESS_EVENTS)
def test_fatal_precedes_sideband_closure(binary: Path) -> Transcript:
    events = lifecycle_events(binary, "invalid_sideband")
    return assert_failure_tail(
        events,
        "worker sideband read failed: unknown variant `broken`",
        {"kind": "worker_signaled", "signal": signal.SIGKILL},
    )


@requires(WORKER, PROCESS_EVENTS)
def test_stdin_write_failure_retires_worker(binary: Path) -> Transcript:
    events = lifecycle_events(binary, "closed_stdin")
    return assert_failure_tail(
        events,
        "worker stdin write failed:",
        {"kind": "worker_signaled", "signal": signal.SIGKILL},
    )


@requires(WORKER, PROCESS_EVENTS)
def test_drains_sideband_after_exit(binary: Path) -> Transcript:
    return [assert_exit_tail(lifecycle_events(binary, "exit_tail"))]


@requires(WORKER, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_retains_failure_discovered_during_exit_drain(binary: Path) -> Transcript:
    return [assert_exit_tail(lifecycle_events(binary, "exit_invalid"), invalid=True)]


def lifecycle_events(binary: Path, scenario: str) -> list[dict]:
    fixture = Path(__file__).resolve().parents[3] / "fixtures/relay_worker/lifecycle.py"
    with tempfile.TemporaryDirectory() as directory, Events() as exits:
        pid_path = Path(directory) / "worker-pid"
        environment = dict(
            os.environ,
            TMPDIR=directory,
            MCP_CONSOLE_HOME=directory,
            TEST_WORKER_PID=str(pid_path),
        )
        if scenario == "exit_invalid":
            environment[LOADER_VARIABLE] = str(
                build_interposer(Path(directory), "relay_drain_failure")
            )
        process = subprocess.Popen(
            [binary, "worker-relay", sys.executable, fixture, scenario],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        try:
            assert json.loads(read_lines(process.stdout, 1, "worker ready")[0]) == {
                "kind": "ready"
            }
            worker = capture_process_identity(int(pid_path.read_text()))
            exits.watch_process(worker[0])
            process.stdin.write(
                json.dumps(LIFECYCLE_COMMANDS[scenario]).encode() + b"\n"
            )
            process.stdin.flush()
            # Keep input open and downstream output unread until direct-worker
            # exit. Retirement must still deliver the complete sideband tail.
            assert exits.wait(10) == {worker[0]}, "worker did not retire"
            command_input = process.stdin
            process.stdin = None
            try:
                output, errors = process.communicate(timeout=10)
            finally:
                command_input.close()
            assert process.returncode == 0 and errors == b"", (
                process.returncode,
                errors,
            )
            assert live_processes([worker]) == [], "direct worker was not reaped"
            return [json.loads(line) for line in output.splitlines()]
        finally:
            if process.poll() is None:
                stop_process_group(process.pid)
            process.communicate(timeout=10)


if __name__ == "__main__":
    run_this_suite(__file__)
