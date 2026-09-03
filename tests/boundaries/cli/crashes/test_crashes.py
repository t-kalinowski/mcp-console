#!/usr/bin/env -S uv run --script

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import (
    FifoCheckpoint,
    Transcript,
    capture_darwin_process_identity,
    kill_darwin_processes,
    live_darwin_processes,
    run_this_suite,
    signal_darwin_process,
)
from cli._harness import (
    TIMEOUT,
    _build_supervision_interposer,
    _cleanup,
    _command,
    _command_record,
    _observe_process_exit,
    _start_lifetime,
    _wait_for_cleanup,
    _wait_for_process_exit,
    _wait_for_process_state,
    _wait_for_gated_root_and_manager,
)


PLATFORMS = {"darwin"}


def test_owner_monitor_start_failure_preserves_temporary_directory(
    binary: Path,
) -> Transcript:
    arguments = ("sandbox", "--", "python", "-c", "raise SystemExit(23)")

    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        monitor_start_failed = FifoCheckpoint(
            fixture_directory / "owner-monitor-start-failed"
        )
        monitor_start_release = FifoCheckpoint(
            fixture_directory / "owner-monitor-start-release"
        )
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(
                fixture_directory,
                "owner-monitor-start-failure",
            )
        )
        environment["MCP_CONSOLE_TEST_OWNER_MONITOR_START_FAILURE"] = str(
            monitor_start_failed.path
        )
        environment["MCP_CONSOLE_TEST_OWNER_MONITOR_START_RELEASE"] = str(
            monitor_start_release.path
        )
        environment["TMPDIR"] = str(fixture_directory)

        process = subprocess.Popen(
            [binary, *arguments],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        identities = ()
        monitor_start_released = False
        sandbox_temporary_directory: Path | None = None
        try:
            monitor_start_failed.wait("owner manager-monitor start failure")
            launcher = capture_darwin_process_identity(process.pid)
            identities = _wait_for_gated_root_and_manager(launcher)
            temporary_directories = list(
                fixture_directory.glob(f"mcp-console-tmp-{process.pid}-*")
            )
            assert len(temporary_directories) == 1, temporary_directories
            sandbox_temporary_directory = temporary_directories[0]

            monitor_start_release.release()
            monitor_start_released = True
            returncode = process.wait(timeout=TIMEOUT)
            stdout = process.stdout.read().decode("utf-8")
            stderr = process.stderr.read().decode("utf-8")

            assert returncode == 1, (returncode, stderr)
            assert stdout == "", stdout
            assert stderr == (
                "failed to start sandbox manager monitor: "
                "Resource temporarily unavailable (os error 35)\n"
            ), stderr
            assert live_darwin_processes(identities) == []
            assert sandbox_temporary_directory.exists(), (
                "monitor startup failure removed the sandbox temporary directory"
            )
            return [
                {
                    "command": _command(*arguments),
                    "owner_checkpoint": "before manager-monitor creation",
                    "manager_monitor_start": "EAGAIN",
                },
                {
                    "launcher_returncode": returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                    "verified_cleanup": "gated sandbox root and manager",
                    "verified_preservation": "sandbox temp",
                },
            ]
        finally:
            if not monitor_start_released:
                monitor_start_release.release()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=TIMEOUT)
            kill_darwin_processes(identities)
            if sandbox_temporary_directory is not None:
                shutil.rmtree(sandbox_temporary_directory, ignore_errors=True)
            for stream in (process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
            monitor_start_failed.close()
            monitor_start_release.close()


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
        assert signal_darwin_process(lifetime.manager, signal.SIGTERM), (
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
                "manager_signal": "SIGTERM",
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
