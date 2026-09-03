#!/usr/bin/env -S uv run --script

import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import Transcript, run_this_suite, signal_darwin_process
from cli._harness import (
    TIMEOUT,
    _cleanup,
    _command,
    _command_record,
    _observe_process_exit,
    _start_lifetime,
    _wait_for_cleanup,
    _wait_for_process_exit,
    _wait_for_process_state,
)


PLATFORMS = {"darwin"}


def test_launcher_crash_retires_the_sandbox_lifetime(binary: Path) -> Transcript:
    lifetime = _start_lifetime(binary)
    try:
        lifetime.process.kill()
        returncode = lifetime.process.wait(timeout=TIMEOUT)
        survivors = _wait_for_cleanup(lifetime)
        stderr = lifetime.process.stderr.read().decode("utf-8")

        assert returncode == -signal.SIGKILL, returncode
        assert stderr == "", stderr
        assert survivors == [], f"launcher crash leaked sandbox processes: {survivors}"
        assert not lifetime.temporary_directory.exists(), (
            "launcher crash leaked the sandbox temporary directory"
        )
        return [
            _command_record(lifetime),
            {
                "launcher_signal": "SIGKILL",
                "launcher_returncode": returncode,
                "verified_cleanup": "sandbox root, detached descendant, manager, and temp",
            },
        ]
    finally:
        _cleanup(lifetime)


def test_manager_crash_retires_the_sandbox_lifetime(binary: Path) -> Transcript:
    lifetime = _start_lifetime(binary)
    try:
        assert signal_darwin_process(lifetime.manager, signal.SIGKILL), (
            "manager exited before crash injection"
        )
        returncode = lifetime.process.wait(timeout=TIMEOUT)
        stderr = lifetime.process.stderr.read().decode("utf-8")
        _wait_for_process_exit(
            (lifetime.root, lifetime.descendant, lifetime.manager),
            "manager crash leaked sandbox processes",
        )

        assert returncode == 128 + signal.SIGKILL, returncode
        assert stderr == "", stderr
        assert lifetime.temporary_directory.exists(), (
            "manager recovery removed the sandbox temporary directory"
        )
        return [
            _command_record(lifetime),
            {
                "manager_signal": "SIGKILL",
                "launcher_returncode": returncode,
                "verified_cleanup": "sandbox root, detached descendant, and manager",
                "verified_preservation": "sandbox temp",
            },
        ]
    finally:
        _cleanup(lifetime)


def test_manager_crash_with_zombie_root_stops_pinned_group(
    binary: Path,
) -> Transcript:
    lifetime = _start_lifetime(binary, detached=False)
    try:
        assert signal_darwin_process(lifetime.manager, signal.SIGSTOP), (
            "manager exited before stop injection"
        )
        _wait_for_process_state(lifetime.manager, "T", "sandbox manager")

        with _observe_process_exit(lifetime.root) as events:
            lifetime.process.stdin.write(b"exit\n")
            lifetime.process.stdin.close()
            assert events.control(None, 1, TIMEOUT), (
                "sandbox root did not exit while manager was stopped"
            )
        _wait_for_process_state(lifetime.root, "Z", "sandbox root")

        assert signal_darwin_process(lifetime.manager, signal.SIGKILL), (
            "manager exited before crash injection"
        )
        returncode = lifetime.process.wait(timeout=TIMEOUT)
        stderr = lifetime.process.stderr.read().decode("utf-8")
        normalized_stderr = stderr.replace(
            str(lifetime.root[0]),
            "<sandbox root pid>",
        )
        _wait_for_process_exit(
            (lifetime.root, lifetime.descendant, lifetime.manager),
            "sandbox process survived zombie-root manager recovery",
        )

        assert returncode == 1, returncode
        assert "sandbox root" in stderr, stderr
        assert "exited before fallback supervision" in stderr, stderr
        assert lifetime.temporary_directory.exists(), (
            "zombie-root recovery removed the sandbox temporary directory"
        )
        return [
            {
                "command": _command(*lifetime.arguments),
                "stdout": (
                    "<sandbox root pid>\n<same-group descendant pid>\n<sandbox temp>\n"
                ),
                "stderr": normalized_stderr,
            },
            {
                "manager_signal": "SIGSTOP then SIGKILL",
                "verified_root_state": "waitable zombie during recovery",
                "launcher_returncode": returncode,
                "verified_cleanup": "sandbox root, same-group descendant, and manager",
                "verified_preservation": "sandbox temp",
            },
        ]
    finally:
        _cleanup(lifetime)


if __name__ == "__main__":
    run_this_suite(__file__)
