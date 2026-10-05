#!/usr/bin/env -S uv run --script

import json
import os
import re
import signal
import subprocess
import sys
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript
from support.capture import read_lines
from support.checkpoints import FifoCheckpoint
from support.events import Events
from support.native import LOADER_VARIABLE, build_interposer
from support.processes import stop_process_group
from support.requirements import NATIVE_FIXTURES, POSIX, requires
from support.suites import run_this_suite


@requires(POSIX)
def test_resolves_python_version_over_json(binary: Path) -> Transcript:
    root = Path(__file__).resolve().parents[3]
    with (root / "Cargo.toml").open("rb") as source:
        build = tomllib.load(source)["package"]["version"]
    with TemporaryDirectory() as temporary:
        uv = Path(temporary) / "uv"
        uv.write_text(
            """#!/bin/sh
case "$1 $2" in
  'python list')
    printf '%s\\n' '[{"version":"3.12.7","version_parts":{"major":3,"minor":12,"patch":7},"symlink":null,"variant":"default","implementation":"cpython"}]'
    ;;
  'tool run')
    for last in "$@"; do :; done
    printf '%s' /usr/bin/true > "$last"
    ;;
  *) exit 90 ;;
esac
"""
        )
        uv.chmod(0o755)
        process = subprocess.Popen(
            [binary, "resolve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PATH": str(uv.parent)},
        )
        assert process.stdin is not None
        assert process.stdout is not None

        def send(message: object) -> None:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        def receive() -> object:
            return json.loads(process.stdout.readline())

        try:
            send(
                {
                    "Open": {
                        "version": 7,
                        "build": build,
                        "mode": "PythonOnly",
                    }
                }
            )
            hello = receive()
            discovery = receive()
            assert hello == {"Hello": {"version": 7, "build": build}}, hello
            assert discovery["Completed"]["id"] == 0, discovery
            assert discovery["Completed"]["confirmed"] is True, discovery
            send(
                {
                    "Run": {
                        "id": 1,
                        "operation": {"PythonVersion": {"constraints": [">=3.12"]}},
                    }
                }
            )
            resolved = receive()
            assert resolved["Completed"]["result"] == {"Ok": "3.12.7"}, resolved
            assert resolved["Completed"]["confirmed"] is True, resolved
            manifest = {
                "packages": ["six>=1"],
                "python_version": [">=3.12"],
                "exclude_newer": "2026-01-01",
            }
            send(
                {
                    "Run": {
                        "id": 2,
                        "operation": {"Python": {"requirements": manifest, "r": None}},
                    }
                }
            )
            prepared = receive()
            assert prepared["Completed"]["result"] == {
                "Ok": {"python": "/usr/bin/true", "requirements": manifest}
            }, prepared
            assert prepared["Completed"]["confirmed"] is True, prepared
            send("Close")
            assert receive() == "Closed"
            process.stdin.close()
            assert process.wait(timeout=10) == 0, process.stderr.read()
            assert process.stderr.read() == ""
            help_text = subprocess.run(
                [binary, "--help"], capture_output=True, text=True, check=True
            ).stdout
            assert "  resolve" not in help_text
            return [
                {
                    "hidden_command": "resolve",
                    "resolved_python": "3.12.7",
                    "prepared_manifest": prepared["Completed"]["result"]["Ok"][
                        "requirements"
                    ],
                }
            ]
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


@requires(NATIVE_FIXTURES)
def test_close_settles_resolver_observation_before_reaping(binary: Path) -> Transcript:
    return observe_resolver(binary, fail=False)


@requires(NATIVE_FIXTURES)
def test_observation_failure_retires_resolver(binary: Path) -> Transcript:
    return observe_resolver(binary, fail=True)


def observe_resolver(binary: Path, *, fail: bool) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        resolver_pid = root / "resolver-pid"
        checkpoints = {
            name: FifoCheckpoint.create(root / name)
            for name in ("entered", "release", "killed")
        }
        uv = root / "uv"
        uv.write_text(f"#!{sys.executable}\nimport signal\nsignal.pause()\n")
        uv.chmod(0o755)
        environment = {
            **os.environ,
            "PATH": str(root),
            LOADER_VARIABLE: str(build_interposer(root, "child_exit_observation")),
            "MCP_CONSOLE_TEST_OBSERVER_ENTERED": str(root / "entered"),
            "MCP_CONSOLE_TEST_OBSERVER_CANCELLABLE": "1",
            "MCP_CONSOLE_TEST_OBSERVER_PID": str(resolver_pid),
            "MCP_CONSOLE_TEST_OBSERVER_RELEASE": str(root / "release"),
            "MCP_CONSOLE_TEST_CHILD_KILLED": str(root / "killed"),
            "MCP_CONSOLE_TEST_EARLY_REAP": str(root / "early-reap"),
        }
        if fail:
            environment["MCP_CONSOLE_TEST_OBSERVER_FAIL"] = "1"
        with (Path(__file__).resolve().parents[3] / "Cargo.toml").open("rb") as source:
            build = tomllib.load(source)["package"]["version"]
        process = subprocess.Popen(
            [binary, "resolve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=True,
        )
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(
                json.dumps(
                    {
                        "Open": {
                            "version": 7,
                            "build": build,
                            "mode": "PythonOnly",
                        }
                    }
                )
                + "\n"
            )
            process.stdin.flush()
            opened = [
                json.loads(line)
                for line in read_lines(process.stdout, 2, "preparation open")
            ]
            assert opened[0] == {"Hello": {"version": 7, "build": build}}, opened
            assert opened[1]["Completed"]["confirmed"] is True, opened
            process.stdin.write(
                json.dumps(
                    {
                        "Run": {
                            "id": 1,
                            "operation": {"PythonVersion": {"constraints": [">=3.12"]}},
                        }
                    }
                )
                + "\n"
            )
            process.stdin.flush()
            checkpoints["entered"].wait("resolver observation entered")
            if fail:
                checkpoints["release"].release()
                checkpoints["killed"].wait("failed observation terminates the resolver")
                messages = [
                    json.loads(line)
                    for line in read_lines(process.stdout, 1, "failed completion")
                ]
                process.stdin.write('"Close"\n')
                process.stdin.flush()
                messages.extend(
                    json.loads(line)
                    for line in read_lines(process.stdout, 1, "preparation close")
                )
            else:
                process.stdin.write('"Close"\n')
                process.stdin.flush()
                checkpoints["killed"].wait("close terminates the active resolver")
                checkpoints["release"].release()
                messages = [
                    json.loads(line)
                    for line in read_lines(
                        process.stdout, 2, "cancelled completion and close"
                    )
                ]
            _, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
            assert stderr == "", stderr
            assert not (root / "early-reap").exists(), (
                "resolver reaped before observation settled"
            )
            assert messages[-1] == "Closed", messages
            completed = next(
                message["Completed"] for message in messages if "Completed" in message
            )
            assert completed["id"] == 1 and completed["confirmed"] is True, completed
            error = "managed Python version resolution cancelled"
            if fail:
                completed["result"]["Err"] = re.sub(
                    r"child process \d+",
                    "child process PID",
                    completed["result"]["Err"].replace(str(uv), "UV"),
                )
                error = "failed to wait for managed Python version resolver `UV`: failed to observe child process PID exit: Input/output error (os error 5)"
            assert completed == {
                "id": 1,
                "result": {"Err": error},
                "control": None if fail else "Cancelled",
                "confirmed": True,
            }, completed
            return [{"messages": messages}]
        finally:
            try:
                # The resolver leads its own group, outside the resolve owner.
                if resolver_pid.exists():
                    group = int(resolver_pid.read_text())
                    with Events() as exits:
                        try:
                            exits.watch_process(group)
                        except ProcessLookupError:
                            pass
                        else:
                            stop_process_group(group)
                            assert exits.wait(10) == {group}, (
                                "resolver outlived fixture cleanup"
                            )
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
                for checkpoint in checkpoints.values():
                    checkpoint.close()


if __name__ == "__main__":
    run_this_suite(__file__)
