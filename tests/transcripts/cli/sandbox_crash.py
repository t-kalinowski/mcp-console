#!/usr/bin/env -S uv run --script

import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import Transcript, code, run_this_suite


PLATFORMS = {"darwin"}
TIMEOUT = 10


def _command(*arguments: str) -> list[str]:
    return ["mcp-console", *arguments]


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_survivors(pids: list[int], timeout: float) -> list[int]:
    deadline = time.monotonic() + timeout
    survivors = list(pids)
    while survivors and time.monotonic() < deadline:
        survivors = [pid for pid in survivors if _pid_is_alive(pid)]
        if survivors:
            time.sleep(0.01)
    return [pid for pid in survivors if _pid_is_alive(pid)]


def _kill_survivors(pids: list[int]) -> list[int]:
    survivors = [pid for pid in pids if _pid_is_alive(pid)]
    for pid in survivors:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return survivors


def _child_pid_by_name(parent_pid: int, name: str) -> int:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,comm="],
        capture_output=True,
        text=True,
        check=True,
        timeout=TIMEOUT,
    )
    matches = []
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) != 3:
            continue
        pid, parent, command = fields
        if int(parent) == parent_pid and Path(command).name == name:
            matches.append(int(pid))
    assert len(matches) == 1, (parent_pid, name, matches)
    return matches[0]


def _runner_lifetime_processes(launcher_pid: int) -> tuple[int, int]:
    runner_pid = _child_pid_by_name(launcher_pid, "mcp-console-sandbox")
    manager_pid = _child_pid_by_name(runner_pid, "mcp-console-sandbox")
    return runner_pid, manager_pid


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


def test_retires_every_processx_pipeline_stage(binary: Path) -> Transcript:
    # processx 3.9 pipelines contain regular process objects. On Unix, each
    # stage creates its own session, so no stage is contained by the root group.
    # fmt: r
    script = code(r"""
        pipeline <- processx::pipeline$new(
          list(
            c("/bin/sleep", "60"),
            c("/bin/cat")
          ),
          stdout = "|",
          stderr = "|",
          cleanup = FALSE
        )
        writeLines(as.character(pipeline$get_pids()))
        flush.console()
        quit(save = "no", status = 23L, runLast = FALSE)
        """)
    arguments = ("sandbox", "--", "Rscript", "--vanilla", "-e", script)
    result = subprocess.run(
        [binary, *arguments],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )

    pids = [int(line) for line in result.stdout.splitlines()]
    survivors = _kill_survivors(pids)

    assert result.returncode == 23, result
    assert result.stderr == "", result.stderr
    assert len(pids) == 2, pids
    assert survivors == [], f"processx pipeline stages survived: {survivors}"
    return [
        {
            "command": _command(*arguments),
            "exit_code": result.returncode,
            "stdout": "<pipeline stage pid>\n<pipeline stage pid>\n",
        }
    ]


def test_launcher_crash_retires_the_sandbox_lifetime(binary: Path) -> Transcript:
    # A sandbox guarantee should not depend on the outer launcher running its
    # Drop implementations. This case requires a separate lifetime manager or
    # an equivalent OS-enforced owner that survives an uncatchable launcher
    # crash long enough to retire the sandbox generation.
    # fmt: r
    script = code(r"""
        child <- processx::process$new(
          "/bin/sleep", "60", cleanup = FALSE
        )
        writeLines(c(
          as.character(Sys.getpid()),
          as.character(child$get_pid()),
          Sys.getenv("TMPDIR")
        ))
        flush.console()
        Sys.sleep(60)
        """)
    arguments = ("sandbox", "--", "Rscript", "--vanilla", "-e", script)
    process = subprocess.Popen(
        [binary, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    pids: list[int] = []
    temporary_directory: Path | None = None
    try:
        lines = _read_lines(
            process.stdout,
            3,
            "the sandbox root, processx child, and temporary directory",
        )
        pids = [int(lines[0]), int(lines[1])]
        runner_pid, manager_pid = _runner_lifetime_processes(process.pid)
        pids.extend((runner_pid, manager_pid))
        temporary_directory = Path(lines[2])

        os.kill(process.pid, signal.SIGKILL)
        returncode = process.wait(timeout=TIMEOUT)
        survivors = _wait_for_survivors(pids, timeout=5)
        temporary_directory_survived = temporary_directory.exists()

        assert returncode == -signal.SIGKILL, returncode
        assert survivors == [], f"launcher crash leaked sandbox processes: {survivors}"
        assert not temporary_directory_survived, (
            f"launcher crash leaked sandbox temporary directory: {temporary_directory}"
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT)
        _kill_survivors(pids)
        process.stdout.close()
        process.stderr.close()
        if temporary_directory is not None:
            shutil.rmtree(temporary_directory, ignore_errors=True)

    return [
        {
            "command": _command(*arguments),
            "stdout": "<sandbox root pid>\n<processx child pid>\n<sandbox temp>\n",
        },
        {
            "launcher_signal": "SIGKILL",
            "launcher_returncode": returncode,
            "verified_cleanup": "sandbox root, processx child, manager, and temp",
        },
    ]


def test_manager_crash_retires_the_sandbox_lifetime(binary: Path) -> Transcript:
    # This is macOS-backend fault injection. While the launcher and sandbox
    # root remain live, the launcher must recover from loss of the committed
    # host-side manager and retire the observed lifetime itself.
    # fmt: r
    script = code(r"""
        child <- processx::process$new(
          "/bin/sleep", "60", cleanup = FALSE
        )
        writeLines(c(
          as.character(Sys.getpid()),
          as.character(child$get_pid()),
          Sys.getenv("TMPDIR")
        ))
        flush.console()
        Sys.sleep(60)
        """)
    arguments = ("sandbox", "--", "Rscript", "--vanilla", "-e", script)
    process = subprocess.Popen(
        [binary, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    pids: list[int] = []
    temporary_directory: Path | None = None
    try:
        lines = _read_lines(
            process.stdout,
            3,
            "the sandbox root, processx child, and temporary directory",
        )
        pids = [int(lines[0]), int(lines[1])]
        runner_pid, manager_pid = _runner_lifetime_processes(process.pid)
        pids.extend((runner_pid, manager_pid))
        temporary_directory = Path(lines[2])

        os.kill(manager_pid, signal.SIGKILL)
        returncode = process.wait(timeout=TIMEOUT)
        stderr = process.stderr.read().decode("utf-8")
        survivors = _wait_for_survivors(pids, timeout=5)
        temporary_directory_survived = temporary_directory.exists()

        assert returncode == 128 + signal.SIGKILL, returncode
        assert stderr == "", stderr
        assert survivors == [], f"manager crash leaked sandbox processes: {survivors}"
        assert not temporary_directory_survived, (
            f"manager crash leaked sandbox temporary directory: {temporary_directory}"
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT)
        _kill_survivors(pids)
        process.stdout.close()
        process.stderr.close()
        if temporary_directory is not None:
            shutil.rmtree(temporary_directory, ignore_errors=True)

    return [
        {
            "command": _command(*arguments),
            "stdout": "<sandbox root pid>\n<processx child pid>\n<sandbox temp>\n",
        },
        {
            "manager_signal": "SIGKILL",
            "launcher_returncode": returncode,
            "verified_cleanup": "sandbox root, processx child, manager, and temp",
        },
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
