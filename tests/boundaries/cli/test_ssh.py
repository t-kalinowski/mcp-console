#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript
from support.r import r_test_environment
from support.requirements import SANDBOX, WORKER, requires
from support.ssh import CONFIG, bootstrap, read_frame
from support.suites import run_this_suite


def test_invalid_target_configuration(binary: Path) -> Transcript:
    cases = (
        ({}, "target.workspace"),
        ({"workspace": "relative"}, "absolute"),
        ({"workspace": "/", "command": []}, "nonempty"),
        ({"workspace": "/", "command": [""]}, "nonempty"),
    )
    records = []
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        config = workspace / CONFIG
        config.parent.mkdir(parents=True)
        for values, expected in cases:
            config.write_text(
                json.dumps(
                    {
                        "target": {
                            "transport": {"kind": "ssh", "host": "mule"},
                            **values,
                        }
                    }
                )
            )
            result = subprocess.run(
                [binary, "serve", "--no-sandbox"],
                cwd=workspace,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode != 0, result
            assert expected in result.stderr, result.stderr
            assert not result.stdout, result.stdout
            records.append({"target": values, "error": result.stderr})
    return records


def test_bootstrap_framing_errors(binary: Path) -> Transcript:
    cases = (
        (b"\x00\x10\x00\x01", "exceeds"),
        (b"\x00\x00\x00\x04{}", "truncated"),
        (b"\x00\x00\x00\x02{}", "bootstrap"),
    )
    records = []
    for payload, expected in cases:
        result = subprocess.run(
            [binary, "ssh-launch"],
            input=payload,
            capture_output=True,
            timeout=10,
        )
        error = result.stderr.decode()
        assert result.returncode != 0, result
        assert expected in error, error
        records.append({"input": payload.hex(), "error": error})
    return records


@requires(WORKER, SANDBOX)
def test_bootstrap_preserves_following_relay_bytes(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        payload = bootstrap(
            binary,
            root,
            policy={
                "environment": {
                    "R_HOME": environment["R_HOME"],
                    "R_PROFILE_USER": os.devnull,
                }
            },
        )
        evaluate = {
            "kind": "evaluate",
            "language": "r",
            "source": "cat('coalesced frame\\n')",
        }
        process = subprocess.Popen(
            [binary, "ssh-launch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None and process.stdout is not None
        try:
            # One OS write includes bootstrap plus the first relay command.
            assert (
                os.write(
                    process.stdin.fileno(),
                    payload + json.dumps(evaluate).encode() + b"\n",
                )
                == len(payload) + len(json.dumps(evaluate).encode()) + 1
            )
            data = bytearray()
            while b'"completed"' not in data:
                tag, body = read_frame(process.stdout)
                if tag == 1:
                    assert json.loads(body)["version"] == 2
                else:
                    assert tag == 2, (tag, body)
                    data.extend(body)
            events = [json.loads(line) for line in data.splitlines()]
            assert {"kind": "console_output", "data": "coalesced frame\n"} in events, (
                events
            )
            process.stdin.write(b'{"kind":"shutdown","grace_millis":1000}\n')
            process.stdin.flush()
            while True:
                tag, body = read_frame(process.stdout)
                if tag == 3:
                    assert json.loads(body) == {"confirmed": True, "error": None}, body
                    break
            assert process.wait(timeout=10) == 0
            assert process.stderr.read() == b""
            return events
        finally:
            process.stdin.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def test_remote_workspace_and_compatibility_errors(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        file = root / "file"
        file.write_text("not a directory")
        cases = (
            (root / "missing", {}, "cannot access remote target.workspace"),
            (file, {}, "is not a directory"),
            (Path("relative"), {}, "absolute"),
            (root, {"version": 999}, "incompatible SSH bootstrap"),
            (root, {"build": "incompatible-build"}, "incompatible SSH bootstrap"),
        )
        for workspace, values, expected in cases:
            result = subprocess.run(
                [binary, "ssh-launch"],
                input=bootstrap(binary, workspace, **values),
                capture_output=True,
                timeout=10,
            )
            error = result.stderr.decode()
            assert result.returncode != 0 and expected in error, error
            records.append({"error": error.replace(str(root), "<workspace>")})
        assert not (root / "missing").exists()
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
