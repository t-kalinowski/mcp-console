#!/usr/bin/env -S uv run --script

import json
import struct
import subprocess
import os
import signal
import threading
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.requirements import POSIX, requires
from support.processes import (
    capture_process_identity,
    child_process_identities,
    kill_processes,
)
from support.ssh import read_frame
from support.suites import run_this_suite


def frame(tag, body):
    return struct.pack(">BI", tag, len(body)) + body


def hello(binary, **overrides):
    build = subprocess.check_output([binary, "--version"], text=True).split()[1]
    return frame(
        1,
        json.dumps(
            {
                "version": 1,
                "build": build,
                "operation": "ssh-prepare",
                "lease_ms": 1000,
                **overrides,
            }
        ).encode(),
    )


@requires(POSIX)
def test_incompatible_lease_fails_before_discovery(binary):
    records = []
    for override in ({"version": 999}, {"build": "other-build"}, {"lease_ms": 0}):
        result = subprocess.run(
            [binary, "ssh-tunnel", "ssh-prepare"],
            input=hello(binary, **override),
            capture_output=True,
            timeout=10,
        )
        assert result.returncode != 0 and not result.stdout, result
        records.append({"input": override, "stderr": result.stderr.decode()})
    return records


@requires(POSIX)
def test_partial_frame_cannot_delay_remote_expiry(binary):
    process = subprocess.Popen(
        [binary, "ssh-tunnel", "ssh-prepare"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        process.stdin.write(hello(binary))
        process.stdin.flush()
        assert read_frame(process.stdout)[0] == 1
        tag, token = read_frame(process.stdout)
        assert tag == 4
        process.stdin.write(frame(5, token) + struct.pack(">BI", 2, 100) + b"partial")
        process.stdin.flush()
        assert process.wait(timeout=10) != 0
        errors = process.stderr.read().decode()
        assert "lease expired" in errors, errors
        return [{"partial_frame_still_open": True, "stderr": errors}]
    finally:
        process.stdin.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


@requires(POSIX)
def test_repeated_response_does_not_renew_lease(binary):
    process = subprocess.Popen(
        [binary, "ssh-tunnel", "ssh-prepare"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        process.stdin.write(hello(binary))
        process.stdin.flush()
        assert read_frame(process.stdout)[0] == 1
        tag, token = read_frame(process.stdout)
        assert tag == 4
        process.stdin.write(frame(5, token) * 2)
        process.stdin.flush()
        assert process.wait(timeout=10) != 0
        errors = process.stderr.read().decode()
        assert "unexpected SSH lease frame" in errors, errors
        return [{"repeated_response_rejected": True, "stderr": errors}]
    finally:
        process.stdin.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


@requires(POSIX)
def test_shutdown_deadline_is_independent_of_healthy_heartbeats(binary):
    process = subprocess.Popen(
        [binary, "ssh-tunnel", "ssh-prepare"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    children = []
    try:
        process.stdin.write(hello(binary, lease_ms=30000))
        process.stdin.flush()
        assert read_frame(process.stdout)[0] == 1
        tag, token = read_frame(process.stdout)
        assert tag == 4
        process.stdin.write(frame(5, token))
        process.stdin.flush()
        # A new challenge proves that the owner has started its helper.
        tag, token = read_frame(process.stdout)
        assert tag == 4
        children = child_process_identities(capture_process_identity(process.pid))
        assert len(children) == 1, children
        os.kill(children[0][0], signal.SIGSTOP)
        process.stdin.write(frame(5, token) + frame(6, struct.pack(">Q", 0)))
        process.stdin.flush()

        def renew():
            try:
                while True:
                    tag, body = read_frame(process.stdout)
                    if tag == 4:
                        process.stdin.write(frame(5, body))
                        process.stdin.flush()
                    elif tag == 6:
                        process.stdin.write(frame(7, b""))
                        process.stdin.flush()
                        return
            except (OSError, AssertionError):
                pass

        reader = threading.Thread(target=renew, daemon=True)
        reader.start()
        assert process.wait(timeout=10) != 0
        reader.join(timeout=1)
        assert not reader.is_alive()
        errors = process.stderr.read().decode()
        assert "retirement deadline" in errors, errors
        return [{"healthy_heartbeats_cannot_extend_shutdown": True, "stderr": errors}]
    finally:
        kill_processes(children)
        process.stdin.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


if __name__ == "__main__":
    run_this_suite(__file__)
