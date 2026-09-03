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
    build_manager_interposer,
    capture_darwin_process_identity,
    code,
    darwin_child_process_identities,
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


def test_spawns_gated_root_before_manager(binary: Path) -> Transcript:
    arguments = ("sandbox", "--", "python", "-c", "raise SystemExit(23)")

    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        manager_spawn = FifoCheckpoint(fixture_directory / "manager-spawn")
        manager_spawn_release = FifoCheckpoint(
            fixture_directory / "manager-spawn-release"
        )
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(
                fixture_directory,
                "root-before-manager",
            )
        )
        environment["MCP_CONSOLE_TEST_MANAGER_SPAWN"] = str(manager_spawn.path)
        environment["MCP_CONSOLE_TEST_MANAGER_SPAWN_RELEASE"] = str(
            manager_spawn_release.path
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
        identities: list[DarwinProcessIdentity] = []
        manager_released = False
        try:
            manager_spawn.wait("sandbox manager spawn")
            launcher = capture_darwin_process_identity(process.pid)
            children = darwin_child_process_identities(launcher)
            assert len(children) == 1, children
            root = children[0]
            identities.append(root)
            _wait_for_private_startup_gate(root)
            sandbox_temporary_directories = list(
                fixture_directory.glob(f"mcp-console-tmp-{process.pid}-*")
            )
            assert len(sandbox_temporary_directories) == 1, (
                sandbox_temporary_directories
            )
            sandbox_temporary_directory = sandbox_temporary_directories[0]

            manager_spawn_release.release()
            manager_released = True
            returncode = process.wait(timeout=TIMEOUT)
            stdout = process.stdout.read().decode("utf-8")
            stderr = process.stderr.read().decode("utf-8")

            assert returncode == 23, (returncode, stderr)
            assert stdout == "", stdout
            assert stderr == "", stderr
            assert live_darwin_processes(identities) == []
            assert not sandbox_temporary_directory.exists(), (
                "sandbox temporary directory survived normal retirement"
            )
            return [
                {
                    "command": _command(*arguments),
                    "manager_checkpoint": "before spawn",
                    "verified_root_state": "blocked on private startup gate",
                },
                {
                    "launcher_returncode": returncode,
                    "verified_cleanup": "sandbox root and temp",
                },
            ]
        finally:
            if not manager_released:
                manager_spawn_release.release()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=TIMEOUT)
            kill_darwin_processes(identities)
            for stream in (process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
            manager_spawn.close()
            manager_spawn_release.close()


def test_target_starts_after_manager_readiness(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import os

        print(os.environ["TMPDIR"], flush=True)
        """)
    arguments = ("sandbox", "--", "python", "-c", script)

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        ready_sent = FifoCheckpoint(temporary / "ready-sent")
        ready_return = FifoCheckpoint(temporary / "ready-return")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_MANAGER_READY_SENT"] = str(ready_sent.path)
        environment["MCP_CONSOLE_TEST_MANAGER_READY_RETURN"] = str(ready_return.path)
        environment["DYLD_INSERT_LIBRARIES"] = str(build_manager_interposer(temporary))

        process = subprocess.Popen(
            [binary, *arguments],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        manager_released = False
        sandbox_temporary_directory: Path | None = None
        try:
            ready_sent.wait("manager READY delivery")

            readable, _, _ = select.select([process.stdout], [], [], TIMEOUT)
            assert readable, "sandbox target waited for another manager response"
            sandbox_temporary_directory = Path(
                process.stdout.readline().decode("utf-8").strip()
            )
            assert sandbox_temporary_directory.exists()

            ready_return.release()
            manager_released = True
            returncode = process.wait(timeout=TIMEOUT)
            stdout = process.stdout.read().decode("utf-8")
            stderr = process.stderr.read().decode("utf-8")

            assert returncode == 0, (returncode, stderr)
            assert stdout == "", stdout
            assert stderr == "", stderr
            assert not sandbox_temporary_directory.exists(), (
                "sandbox temporary directory survived normal retirement"
            )
            return [
                {
                    "command": _command(*arguments),
                    "manager_checkpoint": "READY delivered with send return held",
                    "stdout": "<sandbox temporary directory>\n",
                },
                {
                    "launcher_returncode": returncode,
                    "verified_target": "started without another manager response",
                    "verified_cleanup": "sandbox root, manager, and temp",
                },
            ]
        finally:
            if not manager_released:
                ready_return.release()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=TIMEOUT)
            if sandbox_temporary_directory is not None:
                shutil.rmtree(sandbox_temporary_directory, ignore_errors=True)
            for stream in (process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
            ready_sent.close()
            ready_return.close()


def test_target_waits_for_manager_adoption(binary: Path) -> Transcript:
    # The manager's owner-identity query is held before it can inspect the root
    # or adopt TMPDIR. The exact root must already be blocked on its private
    # gate.
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
