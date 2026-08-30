#!/usr/bin/env -S uv run --script

import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import Transcript, code, run_this_suite


PLATFORMS = {"darwin"}
TIMEOUT = 10


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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


def test_retires_processx_descendants_across_sessions(binary: Path) -> Transcript:
    # Each processx child calls setsid() on Unix. This fixture creates two nested
    # processx generations so neither descendant remains in the root group.
    # fmt: r
    script = code(r"""
        child_script <- '
        grandchild <- processx::process$new(
          "/bin/sleep", "60", cleanup = FALSE
        )
        writeLines(c(
          as.character(grandchild$get_pid()),
          Sys.getenv("TMPDIR")
        ))
        flush.console()
        Sys.sleep(60)
        '

        child <- processx::process$new(
          "Rscript",
          c("--vanilla", "-e", child_script),
          stdout = "|",
          stderr = "2>&1",
          cleanup = FALSE
        )
        stopifnot(child$poll_io(5000)[["output"]] == "ready")
        child_output <- child$read_output_lines()
        stopifnot(length(child_output) == 2L)
        writeLines(c(as.character(child$get_pid()), child_output))
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

    lines = result.stdout.splitlines()
    assert len(lines) == 3, lines
    pids = [int(lines[0]), int(lines[1])]
    temporary_directory = Path(lines[2])
    survivors = _kill_survivors(pids)

    assert result.returncode == 23, result
    assert result.stderr == "", result.stderr
    assert survivors == [], f"sandbox descendants survived: {survivors}"
    assert not temporary_directory.exists(), (
        f"sandbox temporary directory survived: {temporary_directory}"
    )
    return [
        {
            "command": ["mcp-console", *arguments],
            "exit_code": result.returncode,
            "stdout": "<processx child pid>\n<processx grandchild pid>\n<sandbox temp>\n",
        }
    ]


def test_waits_for_processx_crash_supervision(binary: Path) -> Transcript:
    # processx's crash supervisor observes its parent asynchronously. The
    # sandbox must not return while that supervisor and its child remain live.
    # The child ignores SIGTERM so processx's own fallback would otherwise take
    # up to five seconds before escalating to SIGKILL.
    # fmt: r
    script = code(r"""
        child_script <- '
        import signal
        import time

        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("ready", flush=True)
        time.sleep(60)
        '

        child <- processx::process$new(
          "python",
          c("-c", child_script),
          stdout = "|",
          stderr = "2>&1",
          cleanup = FALSE,
          supervise = TRUE
        )
        stopifnot(child$poll_io(5000)[["output"]] == "ready")
        stopifnot(identical(child$read_output_lines(), "ready"))
        writeLines(c(as.character(Sys.getpid()), as.character(child$get_pid())))
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
    try:
        root_pid, child_pid = [
            int(line)
            for line in _read_lines(
                process.stdout,
                2,
                "the sandbox root and processx child PIDs",
            )
        ]
        supervisor_pid = _child_pid_by_name(root_pid, "supervisor")
        pids = [root_pid, child_pid, supervisor_pid]
        root_group = os.getpgid(root_pid)
        assert os.getpgid(child_pid) != root_group
        assert os.getpgid(supervisor_pid) != root_group

        os.kill(root_pid, signal.SIGKILL)
        returncode = process.wait(timeout=TIMEOUT)
        stderr = process.stderr.read().decode("utf-8")
        survivors = _kill_survivors([child_pid, supervisor_pid])

        assert returncode == 137, returncode
        assert stderr == "", stderr
        assert survivors == [], f"processx-supervised processes survived: {survivors}"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT)
        _kill_survivors(pids)
        process.stdout.close()
        process.stderr.close()

    return [
        {
            "command": ["mcp-console", *arguments],
            "exit_code": returncode,
            "stdout": ("<sandbox root pid>\n<processx-supervised child pid>\n"),
            "verified_descendants": [
                "processx child outside root process group",
                "processx crash supervisor outside root process group",
            ],
        }
    ]


def test_preserves_status_when_sigchld_was_ignored(binary: Path) -> Transcript:
    # Darwin preserves the ignored disposition across exec but clears its
    # no-child-wait state. Exercise the real binary entry point so later
    # supervision changes continue to preserve the command's waitable status.
    # fmt: python
    host_script = code(r"""
        import os
        import signal
        import sys

        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        os.execv(sys.argv[1], sys.argv[1:])
        """)
    arguments = (
        "sandbox",
        "--",
        "python",
        "-c",
        "raise SystemExit(23)",
    )
    result = subprocess.run(
        [sys.executable, "-c", host_script, binary, *arguments],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )

    assert result.returncode == 23, result
    assert result.stdout == "", result.stdout
    assert result.stderr == "", result.stderr
    return [
        {
            "command": ["mcp-console", *arguments],
            "inherited_sigchld": "ignored",
            "exit_code": result.returncode,
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
