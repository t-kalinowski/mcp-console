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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.capture import read_lines as _read_lines
from support.macos import (
    DarwinProcessIdentity,
    capture_darwin_process_identity,
    darwin_child_process_identities,
    darwin_process_waits_for_startup_release,
    kill_darwin_processes,
    live_darwin_processes,
    wait_for_darwin_startup_release,
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


def _build_supervision_interposer(directory: Path, behavior: str) -> Path:
    definitions = {
        "manager-start": "-DMCP_CONSOLE_INTERPOSE_MANAGER_START",
        "root-before-manager": "-DMCP_CONSOLE_INTERPOSE_ROOT_BEFORE_MANAGER",
        "owner-monitor-start-failure": (
            "-DMCP_CONSOLE_INTERPOSE_OWNER_MONITOR_START_FAILURE"
        ),
        "manager-stop-failure": "-DMCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE",
        "denied-sigkill": "-DMCP_CONSOLE_INTERPOSE_DENIED_SIGKILL",
        "failed-recovery-stop": "-DMCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP",
        "failed-root-observer": "-DMCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER",
        "late-cleanup": "-DMCP_CONSOLE_INTERPOSE_LATE_CLEANUP",
        "retirement-cleanup": "-DMCP_CONSOLE_INTERPOSE_RETIREMENT_CLEANUP",
        "retirement-reused-identity": (
            "-DMCP_CONSOLE_INTERPOSE_RETIREMENT_REUSED_IDENTITY"
        ),
        "retirement-exit-race": "-DMCP_CONSOLE_INTERPOSE_RETIREMENT_EXIT_RACE",
    }
    assert behavior in definitions, behavior
    source = directory / "supervision-interposer.c"
    library = directory / "supervision-interposer.dylib"
    fixture = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "native"
        / "sandbox_supervision_interposer.c"
    )
    shutil.copyfile(fixture, source)
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Werror",
            definitions[behavior],
            "-dynamiclib",
            "-o",
            library,
            source,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return library


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


def _wait_for_private_startup_gate(identity: DarwinProcessIdentity) -> None:
    wait_for_darwin_startup_release(identity, "sandbox target")


def _remaining_timeout(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


@contextmanager
def _observe_process_exit(
    identity: DarwinProcessIdentity,
) -> Iterator[select.kqueue]:
    events = select.kqueue()
    events.control(
        [
            select.kevent(
                identity[0],
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
        ],
        0,
        0,
    )
    try:
        yield events
    finally:
        events.close()


def _wait_for_gated_root_and_manager(
    launcher: DarwinProcessIdentity,
) -> tuple[DarwinProcessIdentity, DarwinProcessIdentity]:
    deadline = time.monotonic() + TIMEOUT
    while True:
        children = tuple(darwin_child_process_identities(launcher))
        assert len(children) <= 2, children
        gated = tuple(
            child
            for child in children
            if darwin_process_waits_for_startup_release(child)
        )
        assert len(gated) <= 1, (children, gated)
        if len(children) == 2 and gated:
            root = gated[0]
            manager = next(child for child in children if child != root)
            return root, manager
        assert live_darwin_processes((launcher,)) == [launcher[0]], launcher
        assert time.monotonic() < deadline, (
            "launcher did not expose its gated root and manager"
        )
        time.sleep(0.01)


def _thread_count(identity: DarwinProcessIdentity) -> int | None:
    if not live_darwin_processes((identity,)):
        return None
    result = subprocess.run(
        ["/bin/ps", "-M", "-p", str(identity[0])],
        capture_output=True,
        text=True,
        check=True,
        timeout=TIMEOUT,
    )
    if not live_darwin_processes((identity,)):
        return None
    lines = result.stdout.splitlines()
    assert lines, result.stdout
    return len(lines) - 1


def _wait_for_manager_readiness(lifetime: _SandboxLifetime) -> None:
    # SandboxManager starts its launcher-side monitor thread only after the
    # manager's readiness byte has been received. This is a causal readiness
    # checkpoint, unlike sleeping after discovering the manager process.
    deadline = time.monotonic() + TIMEOUT
    while True:
        thread_count = _thread_count(lifetime.launcher)
        assert thread_count is not None, (
            "sandbox launcher exited before manager readiness"
        )
        assert live_darwin_processes((lifetime.manager,)), (
            "sandbox manager exited before readiness"
        )
        if thread_count >= 2:
            return
        assert time.monotonic() < deadline, "sandbox manager did not become ready"
        time.sleep(0.01)


def _start_lifetime(
    binary: Path,
    environment: dict[str, str] | None = None,
    *,
    detached: bool = True,
    move_root_to_descendant_group: bool = False,
) -> _SandboxLifetime:
    assert not (detached and move_root_to_descendant_group)
    # The detached child leaves the root's session, so cleanup must come from
    # exact descendant observation rather than an inherited process group.
    child_group_option = (
        "process_group=0"
        if move_root_to_descendant_group
        else f"start_new_session={detached!r}"
    )
    root_group_setup = (
        "\n        os.setpgid(0, child.pid)" if move_root_to_descendant_group else ""
    )
    # fmt: python
    script = code(rf"""
        import os
        import subprocess
        import sys

        child = subprocess.Popen(
            ["/bin/sleep", "60"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            {child_group_option},
        ){root_group_setup}
        print(os.getpid())
        print(child.pid)
        print(os.environ["TMPDIR"])
        sys.stdout.flush()
        if sys.stdin.readline() == "exit\n":
            raise SystemExit(23)
        raise SystemExit(24)
        """)
    arguments = ("sandbox", "--", "python", "-c", script)
    process = subprocess.Popen(
        [binary, *arguments],
        env=environment,
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
        if move_root_to_descendant_group:
            assert os.getpgid(root[0]) == descendant[0], (
                "sandbox root did not join its descendant's process group"
            )
        elif detached:
            assert os.getsid(descendant[0]) != os.getsid(root[0]), (
                "sandbox descendant did not leave the root session"
            )
        else:
            assert os.getpgid(descendant[0]) == os.getpgid(root[0]), (
                "sandbox descendant did not remain in the root process group"
            )
        manager = capture_darwin_process_identity(_manager_pid(process.pid))
        identities.append(manager)
        lifetime = _SandboxLifetime(
            process=process,
            arguments=arguments,
            launcher=launcher,
            root=root,
            descendant=descendant,
            manager=manager,
            temporary_directory=temporary_directory,
        )
        _wait_for_manager_readiness(lifetime)
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
