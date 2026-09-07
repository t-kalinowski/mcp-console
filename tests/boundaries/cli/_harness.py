from __future__ import annotations

import fcntl
import os
import pty
import select
import selectors
import shutil
import subprocess
import sys
import termios
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.capture import read_lines as _read_lines
from support.macos import (
    DarwinProcessIdentity,
    capture_darwin_process_identity,
    kill_darwin_processes,
    live_darwin_processes,
)
from support.normalization import code

TIMEOUT = 10


@dataclass
class _SandboxLifetime:
    process: subprocess.Popen[bytes]
    arguments: tuple[str, ...]
    launcher: DarwinProcessIdentity
    root: DarwinProcessIdentity
    descendant: DarwinProcessIdentity
    manager: DarwinProcessIdentity
    temporary_directory: Path


def _watch_process_exits(
    identities: tuple[DarwinProcessIdentity, ...],
) -> tuple[select.kqueue, list[select.kevent]]:
    exit_events = select.kqueue()
    watches = [
        select.kevent(
            identity[0],
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_EXIT,
        )
        for identity in identities
    ]
    assert exit_events.control(watches, 0, 0) == []
    return exit_events, watches


def _assert_launcher_cleanup_barrier(
    exit_events: select.kqueue,
    watches: list[select.kevent],
    launcher: DarwinProcessIdentity,
    cleanup: tuple[DarwinProcessIdentity, ...],
    temporary_directory: Path,
    action: str,
) -> set[int]:
    observed_exits = set()
    cleanup_processes = {identity[0] for identity in cleanup}
    for _ in watches:
        events = exit_events.control(None, 1, TIMEOUT)
        assert len(events) == 1, "owned sandbox lifetime did not exit"
        event = events[0]
        assert event.filter == select.KQ_FILTER_PROC, event
        assert event.fflags & select.KQ_NOTE_EXIT, event
        assert event.ident not in observed_exits, event
        if event.ident == launcher[0]:
            assert observed_exits == cleanup_processes, (
                f"owned sandbox launcher exited before {action} cleanup completed: "
                f"{cleanup_processes - observed_exits}"
            )
            assert not temporary_directory.exists(), (
                f"owned sandbox launcher exited before {action} directory cleanup"
            )
        observed_exits.add(event.ident)
    return observed_exits


def _command(*arguments: str) -> list[str]:
    return ["mcp-console", *arguments]


def _start_with_controlling_terminal(
    arguments: list[str | Path],
    environment: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[bytes], int, str]:
    master, slave = pty.openpty()
    slave_name = os.ttyname(slave)

    def attach_controlling_terminal() -> None:
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        os.tcsetpgrp(slave, os.getpid())

    process = subprocess.Popen(
        arguments,
        env=environment,
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=attach_controlling_terminal,
    )
    os.close(slave)
    assert process.stdout is not None
    assert process.stderr is not None
    return process, master, slave_name


def _manager_pid(launcher_pid: int) -> int:
    deadline = time.monotonic() + TIMEOUT
    while True:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            check=True,
            timeout=TIMEOUT,
        )
        matches = []
        for line in result.stdout.splitlines():
            fields = line.strip().split(maxsplit=2)
            if (
                len(fields) == 3
                and int(fields[1]) == launcher_pid
                and "sandbox-manager" in fields[2]
            ):
                matches.append(int(fields[0]))
        assert len(matches) <= 1, (launcher_pid, matches)
        if matches:
            return matches[0]
        assert time.monotonic() < deadline, "sandbox manager did not start"
        time.sleep(0.01)


def _start_lifetime(
    binary: Path,
    exit_with_parent: int | None = None,
    ignore_sigterm: bool = False,
) -> _SandboxLifetime:
    # The detached child leaves the root's session, so cleanup must come from
    # exact descendant observation rather than an inherited process group.
    # fmt: python
    script = code(r"""
        import os
        import subprocess
        import sys

        child = subprocess.Popen(
            ["/bin/sleep", "60"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(os.getpid())
        print(child.pid)
        print(os.environ["TMPDIR"])
        sys.stdout.flush()
        if sys.stdin.readline() == "exit\n":
            raise SystemExit(23)
        raise SystemExit(24)
        """)
    arguments = ["sandbox"]
    if exit_with_parent is not None:
        arguments.extend(("--exit-with-parent", str(exit_with_parent)))
    arguments.extend(("--", "python", "-c", script))
    recorded_arguments = tuple(arguments)
    launch_arguments: list[str | Path] = [binary, *recorded_arguments]
    if ignore_sigterm:
        # fmt: python
        host_script = code(r"""
            import os
            import signal
            import sys

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.execv(sys.argv[1], sys.argv[1:])
            """)
        launch_arguments = [
            sys.executable,
            "-c",
            host_script,
            binary,
            *recorded_arguments,
        ]
    process = subprocess.Popen(
        launch_arguments,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    identities: list[DarwinProcessIdentity] = []
    temporary_directory: Path | None = None
    try:
        root_pid, descendant_pid, temporary_directory_text = _read_lines(
            process.stdout,
            3,
            "the sandbox root, descendant, and temporary directory",
        )
        temporary_directory = Path(temporary_directory_text)
        root = capture_darwin_process_identity(int(root_pid))
        identities.append(root)
        descendant = capture_darwin_process_identity(int(descendant_pid))
        identities.append(descendant)
        launcher = capture_darwin_process_identity(process.pid)
        assert os.getsid(descendant[0]) != os.getsid(root[0]), (
            "sandbox descendant did not leave the root session"
        )
        manager = capture_darwin_process_identity(_manager_pid(process.pid))
        identities.append(manager)
        lifetime = _SandboxLifetime(
            process=process,
            arguments=recorded_arguments,
            launcher=launcher,
            root=root,
            descendant=descendant,
            manager=manager,
            temporary_directory=temporary_directory,
        )
        return lifetime
    except BaseException as error:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stderr.fileno(), selectors.EVENT_READ)
            stderr_ready = selector.select(0)
        stderr = (
            os.read(process.stderr.fileno(), 4096).decode("utf-8", errors="replace")
            if stderr_ready
            else ""
        )
        error.add_note(
            f"sandbox returncode after setup failure: {process.returncode}\n"
            f"sandbox stderr:\n{stderr}"
        )
        kill_darwin_processes(identities)
        if temporary_directory is not None:
            shutil.rmtree(temporary_directory, ignore_errors=True)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        raise


def _wait_for_cleanup(lifetime: _SandboxLifetime, timeout: float = 5) -> list[int]:
    identities = (lifetime.root, lifetime.descendant, lifetime.manager)
    deadline = time.monotonic() + timeout
    survivors = live_darwin_processes(identities)
    while (
        survivors or lifetime.temporary_directory.exists()
    ) and time.monotonic() < deadline:
        time.sleep(0.01)
        survivors = live_darwin_processes(identities)
    return live_darwin_processes(identities)


def _wait_for_process_exit(
    identities: tuple[DarwinProcessIdentity, ...],
    description: str,
    timeout: float = 5,
) -> list[int]:
    deadline = time.monotonic() + timeout
    survivors = live_darwin_processes(identities)
    while survivors and time.monotonic() < deadline:
        time.sleep(0.01)
        survivors = live_darwin_processes(identities)
    assert survivors == [], f"{description}: {survivors}"
    return survivors


def _cleanup(lifetime: _SandboxLifetime) -> None:
    if lifetime.process.poll() is None:
        lifetime.process.kill()
        lifetime.process.wait(timeout=TIMEOUT)
    identities = (lifetime.root, lifetime.descendant, lifetime.manager)
    kill_darwin_processes(identities)
    _wait_for_process_exit(identities, "sandbox cleanup did not stop all processes")
    shutil.rmtree(lifetime.temporary_directory, ignore_errors=True)
    for stream in (
        lifetime.process.stdin,
        lifetime.process.stdout,
        lifetime.process.stderr,
    ):
        if not stream.closed:
            stream.close()


def _command_record(lifetime: _SandboxLifetime) -> dict[str, object]:
    return {
        "command": _command(*lifetime.arguments),
        "stdout": "<sandbox root pid>\n<detached descendant pid>\n<sandbox temp>\n",
    }
