#!/usr/bin/env -S uv run --script

import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import (
    DarwinProcessIdentity,
    FifoCheckpoint,
    Transcript,
    capture_darwin_process_identity,
    code,
    kill_darwin_processes,
    live_darwin_processes,
    run_this_suite,
    signal_darwin_process,
)
from cli._harness import (
    TIMEOUT,
    _build_supervision_interposer,
    _command,
    _start_with_controlling_terminal,
    _wait_for_gated_root_and_manager,
    _wait_for_private_startup_gate,
)


PLATFORMS = {"darwin"}


def test_target_waits_for_manager_adoption(binary: Path) -> Transcript:
    # The manager's first control read is held before it can consume
    # initialization or adopt TMPDIR. The exact root must already be blocked on
    # its private gate.
    # fmt: python
    script = code(r"""
        import os

        temporary_directory = os.environ["TMPDIR"]
        os.rmdir(temporary_directory)
        raise SystemExit(23)
        """)
    arguments = ("sandbox", "--", "python", "-c", script)

    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        manager_started = FifoCheckpoint(fixture_directory / "manager-started")
        manager_release = FifoCheckpoint(fixture_directory / "manager-release")
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(fixture_directory, "manager-start")
        )
        environment["MCP_CONSOLE_TEST_MANAGER_START"] = str(manager_started.path)
        environment["MCP_CONSOLE_TEST_MANAGER_RELEASE"] = str(manager_release.path)
        environment["TMPDIR"] = str(fixture_directory)

        process = subprocess.Popen(
            [binary, *arguments],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        identities: list[DarwinProcessIdentity] = []
        manager_released = False
        sandbox_temporary_directory: Path | None = None
        try:
            manager_started.wait("manager startup before temporary-directory adoption")
            launcher = capture_darwin_process_identity(process.pid)
            root, manager = _wait_for_gated_root_and_manager(launcher)
            identities.extend((root, manager))
            temporary_directories = list(
                fixture_directory.glob(f"mcp-console-tmp-{process.pid}-*")
            )
            assert len(temporary_directories) == 1, temporary_directories
            sandbox_temporary_directory = temporary_directories[0]
            assert signal_darwin_process(root, signal.SIGCONT), (
                "sandbox target exited before the gate-bypass probe"
            )
            _wait_for_private_startup_gate(root)
            assert sandbox_temporary_directory.exists(), (
                "SIGCONT released the target before manager adoption"
            )

            manager_release.release()
            manager_released = True
            returncode = process.wait(timeout=TIMEOUT)
            stdout = process.stdout.read().decode("utf-8")
            stderr = process.stderr.read().decode("utf-8")

            assert returncode == 23, returncode
            assert stdout == "", stdout
            assert stderr == "", stderr
            assert not sandbox_temporary_directory.exists(), (
                "sandbox target did not remove its temporary directory"
            )
            return [
                {
                    "command": _command(*arguments),
                    "stdout": "",
                },
                {
                    "manager_checkpoint": "before temporary-directory adoption",
                    "verified_root_state": "blocked on private startup gate",
                    "gate_bypass_probe": "SIGCONT",
                },
                {
                    "launcher_returncode": returncode,
                    "verified_target": "removed sandbox temp after manager readiness",
                },
            ]
        finally:
            if not manager_released:
                manager_release.release()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=TIMEOUT)
            kill_darwin_processes(identities)
            if sandbox_temporary_directory is not None:
                shutil.rmtree(sandbox_temporary_directory, ignore_errors=True)
            for stream in (process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
            manager_started.close()
            manager_release.close()


def test_terminal_interrupt_before_manager_readiness_preserves_status(
    binary: Path,
) -> Transcript:
    arguments = ("sandbox", "--", "python", "-c", "raise SystemExit(23)")

    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        manager_started = FifoCheckpoint(fixture_directory / "manager-started")
        manager_release = FifoCheckpoint(fixture_directory / "manager-release")
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(fixture_directory, "manager-start")
        )
        environment["MCP_CONSOLE_TEST_MANAGER_START"] = str(manager_started.path)
        environment["MCP_CONSOLE_TEST_MANAGER_RELEASE"] = str(manager_release.path)
        environment["TMPDIR"] = str(fixture_directory)

        process, master = _start_with_controlling_terminal(
            [binary, *arguments],
            environment,
        )
        identities: list[DarwinProcessIdentity] = []
        manager_released = False
        sandbox_temporary_directory: Path | None = None
        try:
            manager_started.wait("manager startup before readiness")
            launcher = capture_darwin_process_identity(process.pid)
            root, manager = _wait_for_gated_root_and_manager(launcher)
            identities.extend((root, manager))
            temporary_directories = list(
                fixture_directory.glob(f"mcp-console-tmp-{process.pid}-*")
            )
            assert len(temporary_directories) == 1, temporary_directories
            sandbox_temporary_directory = temporary_directories[0]

            assert os.getpgid(root[0]) == root[0]
            assert os.tcgetpgrp(master) == root[0]
            terminal_attributes = termios.tcgetattr(master)
            assert terminal_attributes[3] & termios.ISIG
            assert terminal_attributes[6][termios.VINTR] == b"\x03"
            exit_queue = select.kqueue()
            try:
                exit_watch = select.kevent(
                    root[0],
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_EXIT,
                )
                assert exit_queue.control([exit_watch], 0, 0) == []
                os.write(master, b"\x03")
                exit_events = exit_queue.control(None, 1, TIMEOUT)
                assert len(exit_events) == 1, "sandbox root did not exit"
                exit_event = exit_events[0]
                assert exit_event.ident == root[0], exit_event
                assert exit_event.filter == select.KQ_FILTER_PROC, exit_event
                assert exit_event.fflags & select.KQ_NOTE_EXIT, exit_event
            finally:
                exit_queue.close()

            manager_release.release()
            manager_released = True
            returncode = process.wait(timeout=TIMEOUT)
            stdout = process.stdout.read().decode("utf-8")
            stderr = process.stderr.read().decode("utf-8")
            survivors = live_darwin_processes(identities)

            assert returncode == 130, returncode
            assert stdout == "", stdout
            assert stderr == "", stderr
            assert survivors == [], f"sandbox processes survived: {survivors}"
            assert not sandbox_temporary_directory.exists(), (
                "sandbox temporary directory survived normal retirement"
            )
            return [
                {
                    "command": _command(*arguments),
                    "manager_checkpoint": "before readiness",
                    "terminal_foreground_group": "gated sandbox root",
                },
                {
                    "stdin": "<Ctrl-C>",
                    "root_state_before_manager_release": "exit observed",
                    "launcher_returncode": returncode,
                    "verified_cleanup": "sandbox root, manager, and temp",
                },
            ]
        finally:
            if not manager_released:
                manager_release.release()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=TIMEOUT)
            kill_darwin_processes(identities)
            if sandbox_temporary_directory is not None:
                shutil.rmtree(sandbox_temporary_directory, ignore_errors=True)
            for stream in (process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
            os.close(master)
            manager_started.close()
            manager_release.close()


if __name__ == "__main__":
    run_this_suite(__file__)
