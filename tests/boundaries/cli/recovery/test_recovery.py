#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cli._harness import (
    FifoCheckpoint,
    Path,
    TIMEOUT,
    Transcript,
    _build_supervision_interposer,
    _cleanup,
    _command,
    _command_record,
    _observe_process_exit,
    _remaining_timeout,
    _start_lifetime,
    _wait_for_process_exit,
    _wait_for_process_state,
    live_darwin_processes,
    os,
    re,
    run_this_suite,
    select,
    signal,
    signal_darwin_process,
    subprocess,
    sys,
    tempfile,
    time,
)


PLATFORMS = {"darwin"}


def test_manager_owner_loss_stop_failure_remains_bounded(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        group_stop_failed = FifoCheckpoint(
            fixture_directory / "manager-group-stop-failed"
        )
        root_stop_failed = FifoCheckpoint(
            fixture_directory / "manager-root-stop-failed"
        )
        descendant_observed = FifoCheckpoint(
            fixture_directory / "manager-descendant-observed"
        )
        descendant_signaled = FifoCheckpoint(
            fixture_directory / "manager-descendant-signaled"
        )
        environment = os.environ.copy()
        environment.update(
            {
                "DYLD_INSERT_LIBRARIES": str(
                    _build_supervision_interposer(
                        fixture_directory,
                        "manager-stop-failure",
                    )
                ),
                "MCP_CONSOLE_TEST_MANAGER_GROUP_STOP_FAILURE": str(
                    group_stop_failed.path
                ),
                "MCP_CONSOLE_TEST_MANAGER_ROOT_STOP_FAILURE": str(
                    root_stop_failed.path
                ),
                "MCP_CONSOLE_TEST_MANAGER_DESCENDANT_OBSERVED": str(
                    descendant_observed.path
                ),
                "MCP_CONSOLE_TEST_MANAGER_DESCENDANT_SIGNAL": str(
                    descendant_signaled.path
                ),
            }
        )
        lifetime = _start_lifetime(binary, environment)
        try:
            descendant_observed.wait("manager observation of detached descendant")
            with (
                _observe_process_exit(lifetime.manager) as manager_exit,
                _observe_process_exit(lifetime.descendant) as descendant_exit,
            ):
                deadline = time.monotonic() + TIMEOUT
                assert signal_darwin_process(lifetime.launcher, signal.SIGKILL), (
                    "sandbox launcher exited before owner-loss injection"
                )
                returncode = lifetime.process.wait(timeout=_remaining_timeout(deadline))
                group_stop_failed.wait(
                    "manager process-group stop failure",
                    _remaining_timeout(deadline),
                )
                root_stop_failed.wait(
                    "manager direct-root stop failure",
                    _remaining_timeout(deadline),
                )
                descendant_signaled.wait(
                    "manager tracker descendant signal",
                    _remaining_timeout(deadline),
                )
                descendant_events = descendant_exit.control(
                    None,
                    1,
                    _remaining_timeout(deadline),
                )
                events = manager_exit.control(
                    None,
                    1,
                    _remaining_timeout(deadline),
                )

            assert descendant_events, (
                "sandbox tracker did not retire the observed detached descendant"
            )
            assert descendant_events[0].ident == lifetime.descendant[0], (
                descendant_events[0]
            )
            assert descendant_events[0].filter == select.KQ_FILTER_PROC, (
                descendant_events[0]
            )
            assert descendant_events[0].fflags & select.KQ_NOTE_EXIT, descendant_events[
                0
            ]
            assert events, (
                "sandbox manager did not exit after its bounded cleanup interval"
            )
            assert events[0].ident == lifetime.manager[0], events[0]
            assert events[0].filter == select.KQ_FILTER_PROC, events[0]
            assert events[0].fflags & select.KQ_NOTE_EXIT, events[0]
            assert returncode == -signal.SIGKILL, returncode
            _wait_for_process_exit(
                (lifetime.manager,),
                "sandbox manager remained live after bounded cleanup",
            )
            _wait_for_process_state(
                lifetime.descendant,
                "Z",
                "retired sandbox descendant",
            )
            assert live_darwin_processes((lifetime.root,)) == [lifetime.root[0]], (
                "failed root termination unexpectedly stopped the sandbox root"
            )
            assert lifetime.temporary_directory.exists(), (
                "manager stop failure removed the sandbox temporary directory"
            )
            return [
                _command_record(lifetime),
                {
                    "launcher_signal": "SIGKILL",
                    "manager_group_stop_signal": "EPERM",
                    "manager_root_stop_signal": "EPERM",
                    "verified_bounded_return": "within the cleanup deadline",
                    "verified_cleanup": "observed detached descendant and manager",
                    "verified_preservation": "sandbox root and sandbox temp",
                },
            ]
        finally:
            group_stop_failed.close()
            root_stop_failed.close()
            descendant_observed.close()
            descendant_signaled.close()
            _cleanup(lifetime)


def test_manager_owner_loss_after_root_group_change_remains_bounded(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        root_stop_failed = FifoCheckpoint(
            fixture_directory / "manager-root-stop-failed"
        )
        descendant_observed = FifoCheckpoint(
            fixture_directory / "manager-descendant-observed"
        )
        descendant_signaled = FifoCheckpoint(
            fixture_directory / "manager-descendant-signaled"
        )
        environment = os.environ.copy()
        environment.update(
            {
                "DYLD_INSERT_LIBRARIES": str(
                    _build_supervision_interposer(
                        fixture_directory,
                        "manager-stop-failure",
                    )
                ),
                "MCP_CONSOLE_TEST_MANAGER_ROOT_STOP_FAILURE": str(
                    root_stop_failed.path
                ),
                "MCP_CONSOLE_TEST_MANAGER_DESCENDANT_OBSERVED": str(
                    descendant_observed.path
                ),
                "MCP_CONSOLE_TEST_MANAGER_DESCENDANT_SIGNAL": str(
                    descendant_signaled.path
                ),
            }
        )
        lifetime = _start_lifetime(
            binary,
            environment,
            detached=False,
            move_root_to_descendant_group=True,
        )
        try:
            descendant_observed.wait("manager observation of new-group descendant")
            with (
                _observe_process_exit(lifetime.manager) as manager_exit,
                _observe_process_exit(lifetime.descendant) as descendant_exit,
            ):
                deadline = time.monotonic() + TIMEOUT
                assert signal_darwin_process(lifetime.launcher, signal.SIGKILL), (
                    "sandbox launcher exited before owner-loss injection"
                )
                returncode = lifetime.process.wait(timeout=_remaining_timeout(deadline))
                root_stop_failed.wait(
                    "manager direct-root stop failure",
                    _remaining_timeout(deadline),
                )
                descendant_signaled.wait(
                    "manager tracker descendant signal",
                    _remaining_timeout(deadline),
                )
                descendant_events = descendant_exit.control(
                    None,
                    1,
                    _remaining_timeout(deadline),
                )
                events = manager_exit.control(
                    None,
                    1,
                    _remaining_timeout(deadline),
                )

            assert descendant_events, (
                "sandbox tracker did not retire the observed new-group descendant"
            )
            assert descendant_events[0].ident == lifetime.descendant[0], (
                descendant_events[0]
            )
            assert descendant_events[0].filter == select.KQ_FILTER_PROC, (
                descendant_events[0]
            )
            assert descendant_events[0].fflags & select.KQ_NOTE_EXIT, descendant_events[
                0
            ]
            assert events, (
                "sandbox manager did not exit after its bounded cleanup interval"
            )
            assert events[0].ident == lifetime.manager[0], events[0]
            assert events[0].filter == select.KQ_FILTER_PROC, events[0]
            assert events[0].fflags & select.KQ_NOTE_EXIT, events[0]
            assert returncode == -signal.SIGKILL, returncode
            _wait_for_process_exit(
                (lifetime.manager,),
                "sandbox manager remained live after bounded cleanup",
            )
            _wait_for_process_state(
                lifetime.descendant,
                "Z",
                "retired sandbox descendant",
            )
            assert live_darwin_processes((lifetime.root,)) == [lifetime.root[0]], (
                "failed root termination unexpectedly stopped the sandbox root"
            )
            assert lifetime.temporary_directory.exists(), (
                "manager stop failure removed the sandbox temporary directory"
            )
            command = _command_record(lifetime)
            command["stdout"] = (
                "<sandbox root pid>\n<new-group descendant pid>\n<sandbox temp>\n"
            )
            return [
                command,
                {
                    "root_process_group": "descendant PID",
                    "launcher_signal": "SIGKILL",
                    "manager_old_group_stop": "already empty",
                    "manager_root_stop_signal": "EPERM",
                    "verified_bounded_return": "within the cleanup deadline",
                    "verified_cleanup": "observed new-group descendant and manager",
                    "verified_preservation": "sandbox root and sandbox temp",
                },
            ]
        finally:
            root_stop_failed.close()
            descendant_observed.close()
            descendant_signaled.close()
            _cleanup(lifetime)


def test_manager_recovery_failure_wakes_launcher(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        denied_sigkill = FifoCheckpoint(fixture_directory / "denied-sigkill")
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(fixture_directory, "denied-sigkill")
        )
        environment["MCP_CONSOLE_TEST_DENIED_SIGKILL"] = str(denied_sigkill.path)
        lifetime = _start_lifetime(binary, environment)
        try:
            assert signal_darwin_process(lifetime.manager, signal.SIGKILL), (
                "manager exited before crash injection"
            )
            denied_sigkill.wait("launcher manager-recovery signal denial")
            returncode = lifetime.process.wait(timeout=TIMEOUT)
            stderr = lifetime.process.stderr.read().decode("utf-8")
            normalized_stderr = stderr
            for identity in (lifetime.root, lifetime.descendant):
                normalized_stderr = normalized_stderr.replace(
                    str(identity[0]),
                    "<sandbox process pid>",
                )
            _wait_for_process_exit(
                (lifetime.root, lifetime.descendant, lifetime.manager),
                "sandbox processes survived manager recovery failure",
            )

            assert returncode == 1, returncode
            assert "manager recovery failed" in stderr, stderr
            assert "Operation not permitted" in stderr, stderr
            assert lifetime.temporary_directory.exists(), (
                "manager recovery failure removed the sandbox temporary directory"
            )
            command = _command_record(lifetime)
            command["stderr"] = normalized_stderr
            return [
                command,
                {
                    "manager_signal": "SIGKILL",
                    "manager_recovery_signal": "EPERM",
                    "launcher_returncode": returncode,
                    "verified_cleanup": "sandbox root, detached descendant, and manager",
                    "verified_preservation": "sandbox temp",
                },
            ]
        finally:
            denied_sigkill.close()
            _cleanup(lifetime)


def test_manager_recovery_inspection_failure_stops_root(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        inspection_failed = FifoCheckpoint(fixture_directory / "inspection-failed")
        group_stop_failed = FifoCheckpoint(fixture_directory / "group-stop-failed")
        root_stopped = FifoCheckpoint(fixture_directory / "root-stopped")
        root_stop_release = FifoCheckpoint(fixture_directory / "root-stop-release")
        failure_trigger = fixture_directory / "fail-process-info"
        environment = os.environ.copy()
        environment.update(
            {
                "DYLD_INSERT_LIBRARIES": str(
                    _build_supervision_interposer(
                        fixture_directory,
                        "failed-recovery-stop",
                    )
                ),
                "MCP_CONSOLE_TEST_PROCESS_INFO_FAILURE": str(inspection_failed.path),
                "MCP_CONSOLE_TEST_PROCESS_INFO_FAILURE_TRIGGER": str(failure_trigger),
                "MCP_CONSOLE_TEST_GROUP_STOP_FAILURE": str(group_stop_failed.path),
                "MCP_CONSOLE_TEST_RECOVERY_ROOT_STOPPED": str(root_stopped.path),
                "MCP_CONSOLE_TEST_RECOVERY_ROOT_RELEASE": str(root_stop_release.path),
            }
        )
        lifetime = _start_lifetime(binary, environment)
        root_stop_released = False
        try:
            with _observe_process_exit(lifetime.root) as root_exit:
                failure_trigger.touch()
                assert signal_darwin_process(lifetime.manager, signal.SIGKILL), (
                    "manager exited before crash injection"
                )
                inspection_failed.wait("launcher root-inspection failure")
                group_stop_failed.wait("launcher pinned-group stop failure")
                root_stopped.wait("launcher direct pinned-root termination")
                events = root_exit.control(None, 1, TIMEOUT)
                assert events, "direct pinned-root termination did not stop the root"
                assert events[0].ident == lifetime.root[0], events[0]
                assert events[0].filter == select.KQ_FILTER_PROC, events[0]
                assert events[0].fflags & select.KQ_NOTE_EXIT, events[0]
                root_stop_release.release()
                root_stop_released = True
            returncode = lifetime.process.wait(timeout=TIMEOUT)
            stderr = lifetime.process.stderr.read().decode("utf-8")
            normalized_stderr = stderr.replace(
                str(lifetime.root[0]),
                "<sandbox root pid>",
            )
            assert returncode == 1, returncode
            assert "manager recovery failed" in stderr, stderr
            assert "failed to inspect sandbox process" in stderr, stderr
            assert "failed to stop sandbox process group" in stderr, stderr
            assert "Input/output error" in stderr, stderr
            assert live_darwin_processes((lifetime.root, lifetime.manager)) == [], (
                "sandbox root or manager survived failed recovery inspection"
            )
            assert live_darwin_processes((lifetime.descendant,)) == [
                lifetime.descendant[0]
            ], "failed recovery unexpectedly claimed detached-descendant cleanup"
            assert lifetime.temporary_directory.exists(), (
                "manager inspection failure removed the sandbox temporary directory"
            )
            command = _command_record(lifetime)
            command["stderr"] = normalized_stderr
            return [
                command,
                {
                    "manager_signal": "SIGKILL",
                    "manager_recovery_inspection": "EIO",
                    "manager_recovery_group_stop": "EIO",
                    "launcher_returncode": returncode,
                    "verified_bounded_return": "after direct pinned-root termination",
                    "verified_cleanup": "sandbox root and manager",
                    "verified_preservation": "detached descendant and sandbox temp",
                },
            ]
        finally:
            if not root_stop_released:
                root_stop_release.release()
            inspection_failed.close()
            group_stop_failed.close()
            root_stopped.close()
            root_stop_release.close()
            _cleanup(lifetime)


def test_root_observer_failure_reports_group_cleanup_failure(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        inspection_failed = FifoCheckpoint(temporary / "inspection-failed")
        group_stop_failed = FifoCheckpoint(temporary / "group-stop-failed")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(temporary, "failed-root-observer")
        )
        environment["MCP_CONSOLE_TEST_PROCESS_INFO_FAILURE"] = str(
            inspection_failed.path
        )
        environment["MCP_CONSOLE_TEST_GROUP_STOP_FAILURE"] = str(group_stop_failed.path)
        try:
            result = subprocess.run(
                [binary, "sandbox", "--", "/bin/sleep", "60"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
            )
            inspection_failed.wait("sandbox root-observer failure")
            group_stop_failed.wait("sandbox root-group stop failure")
            stderr = re.sub(
                r"sandbox process \d+", "sandbox process <pid>", result.stderr
            )
            assert result.returncode == 1, result.returncode
            assert "failed to inspect sandbox process" in stderr, stderr
            assert "process-group termination also failed" in stderr, stderr
            return [
                {
                    "command": _command("sandbox", "--", "/bin/sleep", "60"),
                    "stderr": stderr,
                }
            ]
        finally:
            inspection_failed.close()
            group_stop_failed.close()


if __name__ == "__main__":
    run_this_suite(__file__)
