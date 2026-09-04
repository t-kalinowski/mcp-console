#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.capture import read_lines
from support.records import Transcript
from support.suites import run_this_suite

PLATFORMS = {"darwin"}


def test_relay_protocol_is_independent_of_sandbox_launch(binary: Path) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    transcript: Transcript = []
    for sandboxed in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["TMPDIR"] = directory
            target = [str(binary), "worker-relay", sys.executable, str(worker)]
            command = [str(binary), "sandbox", "--", *target] if sandboxed else target
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            try:
                if not sandboxed:
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
                transcript.append(
                    {"launch": "sandbox" if sandboxed else "direct", "events": events}
                )
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
