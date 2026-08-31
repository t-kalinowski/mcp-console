#!/usr/bin/env -S uv run --script

import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import (
    DarwinProcessIdentity,
    Transcript,
    capture_darwin_process_identity,
    code,
    darwin_process_waits_for_control,
    kill_darwin_processes,
    live_darwin_processes,
    run_this_suite,
    signal_darwin_process,
)

PLATFORMS = {"darwin"}
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


def _read_lines(stream: object, count: int, description: str) -> list[str]:
    descriptor = stream.fileno()  # type: ignore[attr-defined]
    output = bytearray()
    deadline = time.monotonic() + TIMEOUT
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        while output.count(b"\n") < count:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"timed out waiting for {description}"
            ready = selector.select(remaining)
            assert ready, f"timed out waiting for {description}"
            chunk = os.read(descriptor, 4096)
            assert chunk, f"sandbox closed before reporting {description}"
            output.extend(chunk)
    lines = output.decode("utf-8").splitlines()
    assert len(lines) == count, (description, lines)
    return lines


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


def _thread_count(identity: DarwinProcessIdentity) -> int | None:
    if not live_darwin_processes((identity,)):
        return None
    result = subprocess.run(
        ["/bin/ps", "-M", "-p", str(identity[0]), "-o", "pid="],
        capture_output=True,
        text=True,
        check=True,
        timeout=TIMEOUT,
    )
    if not live_darwin_processes((identity,)):
        return None
    return len(result.stdout.splitlines())


def _wait_for_manager_readiness(lifetime: _SandboxLifetime) -> None:
    # SandboxManager starts its launcher-side monitor thread only after the
    # manager's readiness byte has been received. This is a causal commitment
    # checkpoint, unlike sleeping after discovering the manager process.
    deadline = time.monotonic() + TIMEOUT
    while True:
        thread_count = _thread_count(lifetime.launcher)
        assert thread_count is not None, "sandbox launcher exited before manager readiness"
        assert live_darwin_processes((lifetime.manager,)), (
            "sandbox manager exited before readiness"
        )
        if thread_count >= 2:
            return
        assert time.monotonic() < deadline, "sandbox manager did not become ready"
        time.sleep(0.01)


def _start_lifetime(binary: Path) -> _SandboxLifetime:
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
    arguments = ("sandbox", "--", "python", "-c", script)
    process = subprocess.Popen(
        [binary, *arguments],
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
            "the sandbox root, detached descendant, and temporary directory",
        )
        launcher = capture_darwin_process_identity(process.pid)
        root = capture_darwin_process_identity(int(root_pid))
        descendant = capture_darwin_process_identity(int(descendant_pid))
        identities.extend((root, descendant))
        temporary_directory = Path(temporary_directory_text)
        assert os.getsid(descendant[0]) != os.getsid(root[0]), (
            "sandbox descendant did not leave the root session"
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
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT)
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
    while (survivors or lifetime.temporary_directory.exists()) and time.monotonic() < deadline:
        time.sleep(0.01)
        survivors = live_darwin_processes(identities)
    return live_darwin_processes(identities)


def _wait_for_manager_disposition(lifetime: _SandboxLifetime) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        assert lifetime.temporary_directory.exists(), (
            "manager removed the temporary directory before owner disposition"
        )
        descendant_live = live_darwin_processes((lifetime.descendant,))
        if not descendant_live and darwin_process_waits_for_control(lifetime.manager):
            return
        time.sleep(0.01)
    raise AssertionError("manager did not await standalone owner disposition")


def _cleanup(lifetime: _SandboxLifetime) -> None:
    if lifetime.process.poll() is None:
        lifetime.process.kill()
        lifetime.process.wait(timeout=TIMEOUT)
    kill_darwin_processes(
        (lifetime.root, lifetime.descendant, lifetime.manager)
    )
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


def test_launcher_crash_retires_the_sandbox_lifetime(binary: Path) -> Transcript:
    lifetime = _start_lifetime(binary)
    try:
        lifetime.process.kill()
        returncode = lifetime.process.wait(timeout=TIMEOUT)
        stderr = lifetime.process.stderr.read().decode("utf-8")
        survivors = _wait_for_cleanup(lifetime)

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
        survivors = _wait_for_cleanup(lifetime)

        assert returncode == 128 + signal.SIGKILL, returncode
        assert stderr == "", stderr
        assert survivors == [], f"manager crash leaked sandbox processes: {survivors}"
        assert not lifetime.temporary_directory.exists(), (
            "manager crash leaked the sandbox temporary directory"
        )
        return [
            _command_record(lifetime),
            {
                "manager_signal": "SIGKILL",
                "launcher_returncode": returncode,
                "verified_cleanup": "sandbox root, detached descendant, manager, and temp",
            },
        ]
    finally:
        _cleanup(lifetime)


def test_launcher_crash_after_root_exit_completes_manager_disposition(
    binary: Path,
) -> Transcript:
    lifetime = _start_lifetime(binary)
    try:
        assert signal_darwin_process(lifetime.launcher, signal.SIGSTOP), (
            "launcher exited before stop injection"
        )
        lifetime.process.stdin.write(b"exit\n")
        lifetime.process.stdin.close()
        _wait_for_manager_disposition(lifetime)

        assert signal_darwin_process(lifetime.launcher, signal.SIGKILL), (
            "launcher exited before crash injection"
        )
        returncode = lifetime.process.wait(timeout=TIMEOUT)
        stderr = lifetime.process.stderr.read().decode("utf-8")
        survivors = _wait_for_cleanup(lifetime)

        assert returncode == -signal.SIGKILL, returncode
        assert stderr == "", stderr
        assert survivors == [], f"sandbox processes survived owner loss: {survivors}"
        assert not lifetime.temporary_directory.exists(), (
            "manager did not complete the temporary-directory disposition"
        )
        return [
            _command_record(lifetime),
            {
                "launcher_signal": "SIGSTOP",
                "verified_manager_state": (
                    "descendant retired; temporary directory retained"
                ),
            },
            {
                "launcher_signal": "SIGKILL",
                "launcher_returncode": returncode,
                "verified_cleanup": "sandbox root, manager, and temp",
            },
        ]
    finally:
        _cleanup(lifetime)


if __name__ == "__main__":
    run_this_suite(__file__)
