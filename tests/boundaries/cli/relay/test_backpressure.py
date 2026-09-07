#!/usr/bin/env -S uv run --script

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.capture import read_lines
from support.checkpoints import FifoCheckpoint
from support.records import Transcript
from support.suites import run_this_suite

PLATFORMS = {"darwin"}


@contextmanager
def stdout_backpressure_environment() -> Iterator[
    tuple[Path, dict[str, str], FifoCheckpoint]
]:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        interposer = root / "relay-stdout-backpressure.dylib"
        subprocess.run(
            [
                "cc",
                "-dynamiclib",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                interposer,
                fixtures / "native" / "relay_stdout_backpressure.c",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        blocked = FifoCheckpoint.create(root / "stdout-blocked")
        environment = os.environ.copy()
        environment["TMPDIR"] = directory
        environment["DYLD_INSERT_LIBRARIES"] = str(interposer)
        environment["MCP_CONSOLE_TEST_STDOUT_BLOCKED"] = str(root / "stdout-blocked")
        try:
            yield root, environment, blocked
        finally:
            blocked.close()


@contextmanager
def backpressured_relay(
    binary: Path, mode: str
) -> Iterator[tuple[subprocess.Popen[str], dict[str, FifoCheckpoint], select.kqueue]]:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with stdout_backpressure_environment() as (root, environment, blocked):
        checkpoints = {
            name: FifoCheckpoint.create(root / name)
            for name in ("worker-interrupted", "worker-shutdown", "worker-exit")
        }
        process = subprocess.Popen(
            [
                binary,
                "worker-relay",
                sys.executable,
                fixtures / "relay_worker" / "backpressured_output.py",
                mode,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=True,
        )
        worker_exit = select.kqueue()
        try:
            assert process.stdin is not None and process.stdout is not None
            ready = read_lines(process.stdout, 1, "relay ready")
            assert [json.loads(line) for line in ready] == [{"kind": "ready"}]
            worker_pid = int((root / "worker-pid").read_text())
            watch = select.kevent(
                worker_pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            assert worker_exit.control([watch], 0, 0) == []
            send(process, {"kind": "evaluate", "language": "r", "source": "output"})
            blocked.wait("relay stdout write returned EAGAIN")
            yield process, checkpoints, worker_exit
        finally:
            # The relay remains our waitable group leader until the bounded wait
            # in the test succeeds. Kill its direct worker too on an early failure.
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.communicate(timeout=10)
            worker_exit.close()
            for checkpoint in checkpoints.values():
                checkpoint.close()


def send(process: subprocess.Popen[str], command: dict[str, object]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(command) + "\n")
    process.stdin.flush()


def retirement_result(
    process: subprocess.Popen[str], worker_exit: select.kqueue
) -> Transcript:
    observed = worker_exit.control(None, 1, 3)
    assert len(observed) == 1, "direct worker did not exit"
    assert observed[0].fflags & select.KQ_NOTE_EXIT, observed
    # Do not read stdout until process exit: draining it would release the bug.
    exit_code = process.wait(timeout=5)
    stdout, stderr = process.communicate(timeout=5)
    assert exit_code == 1, (exit_code, stderr)
    assert stderr == (
        "relay stdout write failed: relay stdout retirement deadline expired\n"
    ), stderr
    output = {"kind": "console_output", "data": "blocked output\n"}
    frame = json.dumps(output, separators=(",", ":")) + "\n"
    assert stdout, stdout
    repeated = frame * (len(stdout) // len(frame) + 1)
    assert stdout == repeated[: len(stdout)], stdout
    # Pipe capacity determines both the repetition count and partial-frame
    # boundary. Preserve the exact repeated event after validating every byte.
    return [
        {
            "stdout_prefix": {"repeated_event": output},
            "exit_code": exit_code,
            "stderr": stderr,
        }
    ]


def test_starts_worker_shutdown_while_relay_stdout_is_backpressured(
    binary: Path,
) -> Transcript:
    with backpressured_relay(binary, "shutdown") as (
        process,
        checkpoints,
        worker_exit,
    ):
        send(process, {"kind": "interrupt", "request_id": 1})
        checkpoints["worker-interrupted"].wait("worker accepted SIGINT")
        send(process, {"kind": "shutdown", "grace_millis": 1000})
        checkpoints["worker-shutdown"].wait("worker accepted shutdown", timeout=3)
        return retirement_result(process, worker_exit)


def test_finishes_natural_worker_exit_while_relay_stdout_is_backpressured(
    binary: Path,
) -> Transcript:
    with backpressured_relay(binary, "natural") as (process, checkpoints, worker_exit):
        checkpoints["worker-exit"].release()
        return retirement_result(process, worker_exit)


def test_resumes_relay_output_after_downstream_backpressure(
    binary: Path,
) -> Transcript:
    with backpressured_relay(binary, "natural") as (process, checkpoints, worker_exit):
        assert process.stdout is not None
        lines = read_lines(process.stdout, 4096, "every buffered worker output record")
        output = {"kind": "console_output", "data": "blocked output\n"}
        assert all(json.loads(line) == output for line in lines), lines
        checkpoints["worker-exit"].release()
        observed = worker_exit.control(None, 1, 3)
        assert len(observed) == 1 and observed[0].fflags & select.KQ_NOTE_EXIT, observed
        assert process.wait(timeout=5) == 0
        stdout, stderr = process.communicate(timeout=5)
        assert stderr == "", stderr
        events = [json.loads(line) for line in stdout.splitlines()]
        assert events == [
            {"kind": "stdout_closed"},
            {"kind": "stderr_closed"},
            {"kind": "worker_sideband_closed"},
            {"kind": "worker_exited", "code": 0},
        ], events
        return [
            {
                "repeated_output": output,
                "output_count": len(lines),
                "events": events,
                "exit_code": process.returncode,
                "stderr": stderr,
            }
        ]


def test_finishes_startup_failure_while_relay_stdout_is_backpressured(
    binary: Path,
) -> Transcript:
    with stdout_backpressure_environment() as (root, environment, blocked):
        reader, writer = os.pipe()
        with os.fdopen(reader, "rb") as output, os.fdopen(writer, "wb") as destination:
            os.set_blocking(writer, False)
            preloaded = 0
            try:
                while True:
                    preloaded += os.write(writer, b"x")
            except BlockingIOError:
                pass
            os.set_blocking(writer, True)
            process = subprocess.Popen(
                [binary, "worker-relay", root / "missing-worker"],
                stdin=subprocess.DEVNULL,
                stdout=destination,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            destination.close()
            try:
                blocked.wait("startup Fatal write returned EAGAIN")
                exit_code = process.wait(timeout=5)
                _, stderr = process.communicate(timeout=5)
                assert exit_code == 1, (exit_code, stderr)
                assert stderr == (
                    "failed to launch worker: No such file or directory (os error 2); "
                    "additionally relay stdout write failed: "
                    "relay stdout retirement deadline expired\n"
                ), stderr
                # The preloaded bytes belong to the fixture. A full pipe leaves
                # no room for any part of the relay's startup Fatal frame.
                assert output.read() == b"x" * preloaded
                return [{"exit_code": exit_code, "stdout": "", "stderr": stderr}]
            finally:
                if process.returncode is None:
                    process.kill()
                process.communicate(timeout=10)


if __name__ == "__main__":
    run_this_suite(__file__)
