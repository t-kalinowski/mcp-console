#!/usr/bin/env -S uv run --script

import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.cli._harness import (
    TIMEOUT,
    _cleanup,
    _command,
    _command_record,
    _start_lifetime,
    _wait_for_cleanup,
    _wait_for_gated_root_and_manager,
)
from support.checkpoints import FifoCheckpoint
from support.macos import (
    DarwinProcessIdentity,
    build_manager_interposer,
    capture_darwin_process_identity,
    kill_darwin_processes,
    live_darwin_processes,
    signal_darwin_process,
    wait_for_darwin_process_state as _wait_for_process_state,
)
from support.records import Transcript
from support.suites import run_this_suite


PLATFORMS = {"darwin"}


def test_pending_signal_at_root_exit_preserves_status(binary: Path) -> Transcript:
    lifetime = _start_lifetime(binary)
    exit_events = select.kqueue()
    launcher_resumed = False
    try:
        root_exit = select.kevent(
            lifetime.root[0],
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_EXIT,
        )
        assert exit_events.control([root_exit], 0, 0) == []
        assert signal_darwin_process(lifetime.launcher, signal.SIGSTOP), (
            "sandbox launcher exited before stop injection"
        )
        _wait_for_process_state(lifetime.launcher, "T", "sandbox launcher")

        assert signal_darwin_process(lifetime.launcher, signal.SIGTERM), (
            "sandbox launcher exited before pending-signal injection"
        )
        lifetime.process.stdin.write(b"exit\n")
        lifetime.process.stdin.close()
        events = exit_events.control(None, 1, TIMEOUT)
        assert len(events) == 1, "sandbox root did not exit while launcher was stopped"
        assert events[0].ident == lifetime.root[0], events[0]
        assert events[0].filter == select.KQ_FILTER_PROC, events[0]
        assert events[0].fflags & select.KQ_NOTE_EXIT, events[0]

        assert signal_darwin_process(lifetime.launcher, signal.SIGCONT), (
            "sandbox launcher exited before resume injection"
        )
        launcher_resumed = True
        returncode = lifetime.process.wait(timeout=TIMEOUT)
        stderr = lifetime.process.stderr.read().decode("utf-8")
        survivors = _wait_for_cleanup(lifetime)

        assert returncode == 23, (returncode, stderr)
        assert stderr == "", stderr
        assert survivors == [], f"sandbox processes survived root exit: {survivors}"
        assert not lifetime.temporary_directory.exists(), (
            "pending launcher signal preserved the sandbox temporary directory"
        )
        return [
            _command_record(lifetime),
            {
                "launcher_signal": "SIGSTOP",
                "pending_launcher_signal": "SIGTERM",
                "root_action": "exit 23",
                "verified_pending_signal": "before launcher resume",
            },
            {
                "launcher_signal": "SIGCONT",
                "launcher_returncode": returncode,
                "verified_signal": "consumed without replacing root status",
                "verified_cleanup": (
                    "sandbox root, detached descendant, manager, and temp"
                ),
            },
        ]
    finally:
        if not launcher_resumed:
            signal_darwin_process(lifetime.launcher, signal.SIGCONT)
        exit_events.close()
        _cleanup(lifetime)


def test_pending_signal_during_manager_crash_before_gate_release_preserves_status(
    binary: Path,
) -> Transcript:
    arguments = ("sandbox", "--", "python", "-c", "raise SystemExit(23)")

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        gate_ready = FifoCheckpoint.create(temporary / "gate-ready")
        gate_release = FifoCheckpoint.create(temporary / "gate-release")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_OWNER_GATE_READY"] = str(gate_ready.path)
        environment["MCP_CONSOLE_TEST_OWNER_GATE_RELEASE"] = str(gate_release.path)
        environment["DYLD_INSERT_LIBRARIES"] = str(build_manager_interposer(temporary))
        process = subprocess.Popen(
            [binary, *arguments],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        identities: tuple[DarwinProcessIdentity, ...] = ()
        sandbox_temporary_directory: Path | None = None
        gate_released = False
        root_exit = select.kqueue()
        try:
            gate_ready.wait("owner startup-gate release")
            launcher = capture_darwin_process_identity(process.pid)
            root, manager = _wait_for_gated_root_and_manager(launcher)
            identities = (root, manager)
            sandbox_directories = tuple(
                temporary.glob(f"mcp-console-tmp-{process.pid}-*")
            )
            assert len(sandbox_directories) == 1, sandbox_directories
            sandbox_temporary_directory = sandbox_directories[0]

            exit_watch = select.kevent(
                root[0],
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_EXIT,
            )
            assert root_exit.control([exit_watch], 0, 0) == []
            assert signal_darwin_process(launcher, signal.SIGTERM), (
                "sandbox launcher exited before pending-signal injection"
            )
            assert signal_darwin_process(manager, signal.SIGKILL), (
                "manager exited before gate-release failure injection"
            )
            exit_events = root_exit.control(None, 1, TIMEOUT)
            assert len(exit_events) == 1, (
                "manager recovery did not stop the gated sandbox root"
            )
            assert exit_events[0].ident == root[0], exit_events[0]

            gate_release.release()
            gate_released = True
            returncode = process.wait(timeout=TIMEOUT)
            stdout = process.stdout.read().decode("utf-8")
            stderr = process.stderr.read().decode("utf-8")
            survivors = live_darwin_processes(identities)

            assert returncode == 137, (returncode, stderr)
            assert stdout == "", stdout
            assert stderr == "", stderr
            assert survivors == [], f"sandbox processes survived: {survivors}"
            assert sandbox_temporary_directory.exists(), (
                "manager recovery removed sandbox temporary directory"
            )
            return [
                {
                    "command": _command(*arguments),
                    "manager_checkpoint": "before startup-gate release",
                    "pending_launcher_signal": "SIGTERM",
                    "manager_signal": "SIGKILL",
                },
                {
                    "launcher_returncode": returncode,
                    "stderr": stderr,
                    "verified_signal": "consumed without replacing root status",
                    "verified_cleanup": "gated sandbox root and manager",
                    "verified_preservation": "sandbox temp",
                },
            ]
        finally:
            if not gate_released:
                gate_release.release()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=TIMEOUT)
            kill_darwin_processes(identities)
            if sandbox_temporary_directory is not None:
                shutil.rmtree(sandbox_temporary_directory, ignore_errors=True)
            for stream in (process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
            root_exit.close()
            gate_ready.close()
            gate_release.close()


if __name__ == "__main__":
    run_this_suite(__file__)
