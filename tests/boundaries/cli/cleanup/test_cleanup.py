#!/usr/bin/env -S uv run --script

import os
import select
import signal
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.cli._harness import (
    TIMEOUT,
    _build_supervision_interposer,
    _cleanup,
    _command_record,
    _start_lifetime,
    _thread_count,
    _wait_for_cleanup,
    _wait_for_process_exit,
)
from support.checkpoints import FifoCheckpoint
from support.macos import live_darwin_processes, signal_darwin_process
from support.records import Transcript
from support.suites import run_this_suite


PLATFORMS = {"darwin"}


def test_cleanup_signal_after_root_exit_terminates_launcher(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        late_cleanup = FifoCheckpoint.create(fixture_directory / "late-cleanup")
        late_cleanup_release = FifoCheckpoint.create(
            fixture_directory / "late-cleanup-release"
        )
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(fixture_directory, "late-cleanup")
        )
        environment["MCP_CONSOLE_TEST_LATE_CLEANUP"] = str(late_cleanup.path)
        environment["MCP_CONSOLE_TEST_LATE_CLEANUP_RELEASE"] = str(
            late_cleanup_release.path
        )
        lifetime = _start_lifetime(binary, environment)
        cleanup_released = False
        try:
            lifetime.process.stdin.write(b"exit\n")
            lifetime.process.stdin.close()
            late_cleanup.wait("manager cleanup after sandbox root exit")
            assert signal_darwin_process(lifetime.launcher, signal.SIGTERM), (
                "launcher exited before cleanup signal"
            )
            returncode = lifetime.process.wait(timeout=TIMEOUT)
            stderr = lifetime.process.stderr.read().decode("utf-8")
            late_cleanup_release.release()
            cleanup_released = True
            _wait_for_process_exit(
                (lifetime.root, lifetime.descendant, lifetime.manager),
                "sandbox processes survived cleanup-time launcher signal",
            )

            assert returncode == -signal.SIGTERM, returncode
            assert stderr == "", stderr
            assert not lifetime.temporary_directory.exists(), (
                "cleanup-time signal preserved the sandbox temporary directory"
            )
            return [
                _command_record(lifetime),
                {
                    "manager_cleanup": "completion held after root exit",
                    "launcher_signal": "SIGTERM",
                    "launcher_returncode": returncode,
                    "verified_cleanup": "sandbox root, detached descendant, and manager",
                    "verified_removal": "sandbox temp",
                },
            ]
        finally:
            if not cleanup_released:
                late_cleanup_release.release()
            late_cleanup.close()
            late_cleanup_release.close()
            _cleanup(lifetime)


def test_cleanup_timeout_preserves_temporary_directory(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        late_cleanup = FifoCheckpoint.create(fixture_directory / "late-cleanup")
        late_cleanup_release = FifoCheckpoint.create(
            fixture_directory / "late-cleanup-release"
        )
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(fixture_directory, "late-cleanup")
        )
        environment["MCP_CONSOLE_TEST_LATE_CLEANUP"] = str(late_cleanup.path)
        environment["MCP_CONSOLE_TEST_LATE_CLEANUP_RELEASE"] = str(
            late_cleanup_release.path
        )
        lifetime = _start_lifetime(binary, environment)
        try:
            lifetime.process.stdin.write(b"exit\n")
            lifetime.process.stdin.close()
            late_cleanup.wait("manager cleanup completion")
            returncode = lifetime.process.wait(timeout=TIMEOUT)
            stderr = lifetime.process.stderr.read().decode("utf-8")
            normalized_stderr = stderr.replace(
                str(lifetime.root[0]),
                "<sandbox root pid>",
            )
            _wait_for_process_exit(
                (lifetime.root, lifetime.descendant, lifetime.manager),
                "sandbox processes survived delayed cleanup completion",
            )

            assert returncode == 1, (returncode, stderr)
            assert "timed out waiting for sandbox manager cleanup" in stderr, stderr
            assert "manager recovery failed" in stderr, stderr
            assert lifetime.temporary_directory.exists(), (
                "cleanup timeout removed the sandbox temporary directory"
            )
            command = _command_record(lifetime)
            command["stderr"] = normalized_stderr
            return [
                command,
                {
                    "manager_cleanup": "completion held past launcher timeout",
                    "launcher_returncode": returncode,
                    "verified_cleanup": "sandbox root, detached descendant, and manager",
                    "verified_preservation": "sandbox temp",
                },
            ]
        finally:
            late_cleanup_release.release()
            late_cleanup.close()
            late_cleanup_release.close()
            _cleanup(lifetime)


def test_manager_stop_failure_remains_bounded(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        late_cleanup = FifoCheckpoint.create(fixture_directory / "late-cleanup")
        late_cleanup_release = FifoCheckpoint.create(
            fixture_directory / "late-cleanup-release"
        )
        denied_sigkill = FifoCheckpoint.create(fixture_directory / "denied-sigkill")
        root_reaped = FifoCheckpoint.create(fixture_directory / "root-reaped")
        root_reap_release = FifoCheckpoint.create(
            fixture_directory / "root-reap-release"
        )
        late_recovery = FifoCheckpoint.create(fixture_directory / "late-recovery")
        late_recovery_release = FifoCheckpoint.create(
            fixture_directory / "late-recovery-release"
        )
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(
                fixture_directory,
                "late-cleanup",
            )
        )
        environment["MCP_CONSOLE_TEST_LATE_CLEANUP"] = str(late_cleanup.path)
        environment["MCP_CONSOLE_TEST_LATE_CLEANUP_RELEASE"] = str(
            late_cleanup_release.path
        )
        environment["MCP_CONSOLE_TEST_DENIED_SIGKILL"] = str(denied_sigkill.path)
        environment["MCP_CONSOLE_TEST_ROOT_REAPED"] = str(root_reaped.path)
        environment["MCP_CONSOLE_TEST_ROOT_REAP_RELEASE"] = str(root_reap_release.path)
        environment["MCP_CONSOLE_TEST_LATE_RECOVERY"] = str(late_recovery.path)
        environment["MCP_CONSOLE_TEST_LATE_RECOVERY_RELEASE"] = str(
            late_recovery_release.path
        )
        lifetime = _start_lifetime(binary, environment)
        root_reap_released = False
        try:
            lifetime.process.stdin.write(b"exit\n")
            lifetime.process.stdin.close()
            late_cleanup.wait("cleanup completion after launcher timeout")
            denied_sigkill.wait("launcher manager-stop signal denial")
            root_reaped.wait("root reap after the second manager deadline")
            assert signal_darwin_process(lifetime.manager, signal.SIGKILL), (
                "manager exited before late monitor shutdown"
            )

            deadline = time.monotonic() + TIMEOUT
            while True:
                thread_count = _thread_count(lifetime.launcher)
                if thread_count == 1:
                    break
                remaining = deadline - time.monotonic()
                assert remaining > 0, (
                    "detached sandbox manager monitor did not stop: "
                    f"launcher has {thread_count} threads"
                )
                readable, _, _ = select.select(
                    [late_recovery.descriptor],
                    [],
                    [],
                    min(0.05, remaining),
                )
                assert not readable, (
                    "detached monitor inspected the root after its PID pin was released"
                )

            root_reap_release.release()
            root_reap_released = True
            returncode = lifetime.process.wait(timeout=TIMEOUT)
            stderr = lifetime.process.stderr.read().decode("utf-8")
            normalized_stderr = stderr.replace(
                str(lifetime.manager[0]),
                "<sandbox manager pid>",
            )

            assert returncode == 1, returncode
            assert "timed out waiting for sandbox manager cleanup" in stderr, stderr
            assert "failed to stop sandbox manager" in stderr, stderr
            assert "Operation not permitted" in stderr, stderr
            _wait_for_process_exit(
                (lifetime.root, lifetime.descendant, lifetime.manager),
                "sandbox processes survived late manager shutdown",
            )
            assert lifetime.temporary_directory.exists(), (
                "manager-stop failure removed the sandbox temporary directory"
            )
            command = _command_record(lifetime)
            command["stderr"] = normalized_stderr
            return [
                command,
                {
                    "manager_cleanup": "completion held past both owner deadlines",
                    "manager_stop_signal": "EPERM",
                    "launcher_returncode": returncode,
                    "verified_bounded_return": "within the recovery deadline",
                    "verified_detached_monitor": "no recovery after root reap",
                    "verified_cleanup": "sandbox root, detached descendant, and manager",
                    "verified_preservation": "sandbox temp",
                },
            ]
        finally:
            late_cleanup_release.release()
            late_recovery_release.release()
            if not root_reap_released:
                root_reap_release.release()
            late_cleanup.close()
            late_cleanup_release.close()
            denied_sigkill.close()
            root_reaped.close()
            root_reap_release.close()
            late_recovery.close()
            late_recovery_release.close()
            _cleanup(lifetime)


def test_launcher_crash_during_retirement_removes_temporary_directory(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        cleanup = FifoCheckpoint.create(fixture_directory / "retirement-cleanup")
        cleanup_release = FifoCheckpoint.create(
            fixture_directory / "retirement-cleanup-release"
        )
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(
                fixture_directory,
                "retirement-cleanup",
            )
        )
        environment["MCP_CONSOLE_TEST_RETIREMENT_CLEANUP"] = str(cleanup.path)
        environment["MCP_CONSOLE_TEST_RETIREMENT_RELEASE"] = str(cleanup_release.path)
        lifetime = _start_lifetime(binary, environment)
        cleanup_released = False
        try:
            lifetime.process.stdin.write(b"exit\n")
            lifetime.process.stdin.close()
            cleanup.wait("manager retirement cleanup")
            _wait_for_process_exit(
                (lifetime.descendant,),
                "detached descendant survived retirement",
            )
            assert live_darwin_processes((lifetime.manager,)) == [
                lifetime.manager[0]
            ], "manager exited before retirement cleanup completed"
            assert lifetime.temporary_directory.exists(), (
                "temporary directory disappeared before retirement cleanup completed"
            )

            assert signal_darwin_process(lifetime.launcher, signal.SIGKILL), (
                "launcher exited before crash injection"
            )
            returncode = lifetime.process.wait(timeout=TIMEOUT)
            stderr = lifetime.process.stderr.read().decode("utf-8")
            cleanup_release.release()
            cleanup_released = True
            _wait_for_process_exit(
                (lifetime.root, lifetime.descendant, lifetime.manager),
                "sandbox processes survived owner loss during retirement",
            )
            _wait_for_cleanup(lifetime)

            assert returncode == -signal.SIGKILL, returncode
            assert stderr == "", stderr
            assert not lifetime.temporary_directory.exists(), (
                "manager preserved the temporary directory after successful cleanup"
            )
            return [
                _command_record(lifetime),
                {
                    "manager_checkpoint": "retirement cleanup",
                    "verified_manager_state": "cleanup complete before directory removal",
                    "verified_cleanup": "detached descendant",
                },
                {
                    "launcher_signal": "SIGKILL",
                    "launcher_returncode": returncode,
                    "verified_cleanup": "sandbox root and manager",
                    "verified_removal": "sandbox temp",
                },
            ]
        finally:
            if not cleanup_released:
                cleanup_release.release()
            cleanup.close()
            cleanup_release.close()
            _cleanup(lifetime)


if __name__ == "__main__":
    run_this_suite(__file__)
