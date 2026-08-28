#!/usr/bin/env -S uv run --script

import fcntl
import os
import pty
import selectors
import signal
import subprocess
import sys
import termios
import time
from pathlib import Path
from tempfile import TemporaryDirectory

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


def _kill_survivors(pids: list[int]) -> list[int]:
    survivors = [pid for pid in pids if _pid_is_alive(pid)]
    for pid in survivors:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return survivors


def _kill_process_groups(process_groups: list[int | None]) -> None:
    for process_group in {group for group in process_groups if group is not None}:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _open_controlling_terminal() -> tuple[int, int, object]:
    master, slave = pty.openpty()

    def attach_controlling_terminal() -> None:
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        os.tcsetpgrp(slave, os.getpid())

    return master, slave, attach_controlling_terminal


def _wait_for_stop(process_id: int) -> int:
    deadline = time.monotonic() + TIMEOUT
    while True:
        waited, status = os.waitpid(process_id, os.WUNTRACED | os.WNOHANG)
        if waited == process_id:
            assert os.WIFSTOPPED(status), status
            return status
        assert time.monotonic() < deadline, "timed out waiting for launcher stop"
        time.sleep(0.01)


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


def _read_until(
    descriptor: int,
    markers: bytes | tuple[bytes, ...],
    description: str,
) -> bytes:
    if isinstance(markers, bytes):
        markers = (markers,)
    output = bytearray()
    deadline = time.monotonic() + TIMEOUT
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        while not all(marker in output for marker in markers):
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"timed out waiting for {description}"
            ready = selector.select(remaining)
            assert ready, f"timed out waiting for {description}"
            chunk = os.read(descriptor, 4096)
            assert chunk, f"terminal closed before reporting {description}"
            output.extend(chunk)
    return bytes(output)


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
            "command": _command(*arguments),
            "exit_code": result.returncode,
            "stdout": "<processx child pid>\n<processx grandchild pid>\n<sandbox temp>\n",
        }
    ]


def test_retires_same_group_child_forked_before_root_exit(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import os
        import time

        child = os.fork()
        if child == 0:
            os.close(1)
            os.close(2)
            time.sleep(60)
            os._exit(0)

        os.write(1, f"{child}\n".encode())
        os._exit(0)
        """)
    arguments = ("sandbox", "--", "python", "-c", script)
    result = subprocess.run(
        [binary, *arguments],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )

    pid = int(result.stdout.strip())
    deadline = time.monotonic() + 1
    while _pid_is_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    survivors = _kill_survivors([pid])

    assert result.returncode == 0, result
    assert result.stderr == "", result.stderr
    assert survivors == [], f"same-group sandbox child survived: {survivors}"
    return [
        {
            "command": _command(*arguments),
            "stdout": "<same-group child pid>\n",
        }
    ]


def test_relays_interrupt_then_retires_descendants(binary: Path) -> Transcript:
    # fmt: r
    script = code(r"""
        child <- processx::process$new(
          "/bin/sleep", "60", cleanup = FALSE
        )
        writeLines(c(
          as.character(Sys.getpid()),
          as.character(child$get_pid())
        ))
        flush.console()
        tryCatch(
          Sys.sleep(60),
          interrupt = function(...) {
            quit(save = "no", status = 130L, runLast = FALSE)
          }
        )
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
        pids = [
            int(line)
            for line in _read_lines(
                process.stdout,
                2,
                "the sandbox root and processx descendant PIDs",
            )
        ]
        os.kill(process.pid, signal.SIGINT)
        returncode = process.wait(timeout=TIMEOUT)
        stderr = process.stderr.read().decode("utf-8")
        survivors = _kill_survivors(pids)

        assert returncode == 130, returncode
        assert stderr == "", stderr
        assert survivors == [], f"interrupted sandbox processes survived: {survivors}"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT)
        _kill_survivors(pids)
        process.stdout.close()
        process.stderr.close()

    return [
        {
            "command": _command(*arguments),
            "stdout": "<sandbox root pid>\n<processx child pid>\n",
        },
        {
            "signal": "SIGINT",
            "exit_code": returncode,
            "stderr": stderr,
        },
    ]


def test_closes_unlisted_inherited_descriptors(binary: Path) -> Transcript:
    # fmt: python
    sandboxed_script = code(r"""
        import errno
        import os
        import sys

        try:
            os.write(int(sys.argv[1]), b"escaped")
        except OSError as error:
            assert error.errno == errno.EBADF
        else:
            raise SystemExit("unlisted inherited descriptor remained writable")

        print("closed")
        """)
    arguments = ("sandbox", "--", "python", "-c", sandboxed_script)

    with TemporaryDirectory() as directory:
        host_file = Path(directory) / "host.txt"
        host_file.write_bytes(b"")
        with host_file.open("ab", buffering=0) as stream:
            descriptor = fcntl.fcntl(stream.fileno(), fcntl.F_DUPFD, 64)
            os.set_inheritable(descriptor, True)
            try:
                result = subprocess.run(
                    [binary, *arguments, str(descriptor)],
                    pass_fds=(descriptor,),
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT,
                )
            finally:
                os.close(descriptor)
        escaped = host_file.read_bytes()

    assert result.returncode == 0, result
    assert result.stdout == "closed\n", result.stdout
    assert result.stderr == "", result.stderr
    assert escaped == b"", escaped
    return [
        {
            "command": [*_command(*arguments), "<inherited fd>"],
            "stdout": result.stdout,
        }
    ]


def test_delivers_terminal_interrupt_once(binary: Path) -> Transcript:
    # fmt: python
    sandboxed_script = code(r"""
        import signal
        import time

        interrupts = 0

        def handle_interrupt(_signal, _frame):
            global interrupts
            interrupts += 1

        signal.signal(signal.SIGINT, handle_interrupt)
        print("ready", flush=True)
        print(input(), flush=True)
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            time.sleep(0.01)
        print(interrupts)
        """)
    arguments = ("sandbox", "--", "python", "-c", sandboxed_script)
    master, slave, attach = _open_controlling_terminal()
    process = subprocess.Popen(
        [binary, *arguments],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=attach,
    )
    os.close(slave)
    sandbox_group = None
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == "ready\n"
        sandbox_group = os.tcgetpgrp(master)
        os.write(master, b"sandbox input\n")
        assert process.stdout.readline() == "sandbox input\n"
        os.write(master, b"\x03")
        stdout, stderr = process.communicate(timeout=5)
    except BaseException:
        _kill_process_groups([sandbox_group, process.pid])
        process.wait(timeout=TIMEOUT)
        raise
    finally:
        os.close(master)

    assert process.returncode == 0, process.returncode
    assert stdout == "1\n", stdout
    assert stderr == "", stderr
    return [
        {
            "command": _command(*arguments),
            "stdin": "sandbox input\n<Ctrl-C>",
            "stdout": stdout,
        }
    ]


def test_stops_and_continues_foreground_sandbox_job(binary: Path) -> Transcript:
    # fmt: python
    sandboxed_script = code(r"""
        import signal

        def continued(_signal, _frame):
            print("continued", flush=True)

        def interrupted(_signal, _frame):
            raise SystemExit(0)

        signal.signal(signal.SIGCONT, continued)
        signal(signal.SIGINT, interrupted)
        print("ready", flush=True)
        while True:
            signal.pause()
        """)
    arguments = ("sandbox", "--", "python", "-c", sandboxed_script)
    master, slave, attach = _open_controlling_terminal()
    process = subprocess.Popen(
        [binary, *arguments],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=attach,
    )
    os.close(slave)
    sandbox_group = None
    try:
        assert process.stdout is not None
        ready = process.stdout.readline()
        assert ready == "ready\n"
        sandbox_group = os.tcgetpgrp(master)
        os.write(master, b"\x1a")
        _wait_for_stop(process.pid)
        assert os.tcgetpgrp(master) == process.pid

        os.killpg(process.pid, signal.SIGCONT)
        continued = process.stdout.readline()
        assert continued == "continued\n"
        assert os.tcgetpgrp(master) == sandbox_group
        os.write(master, b"\x03")
        stdout, stderr = process.communicate(timeout=5)
    except BaseException:
        _kill_process_groups([sandbox_group, process.pid])
        process.wait(timeout=TIMEOUT)
        raise
    finally:
        os.close(master)

    assert process.returncode == 0, process.returncode
    assert stdout == "", stdout
    assert stderr == "", stderr
    return [
        {
            "command": _command(*arguments),
            "stdout": ready,
            "stdin": "<Ctrl-Z>",
        },
        {"launcher": "stopped"},
        {
            "signal": "SIGCONT",
            "stdout": continued,
            "stdin": "<Ctrl-C>",
            "exit_code": process.returncode,
        },
    ]


def test_preserves_foreground_pipeline_job_control(binary: Path) -> Transcript:
    # Delay the first stage's exec so the shell has created the downstream peer
    # in the same foreground process group before MCP Console inspects it.
    # fmt: python
    sandboxed_script = code(r"""
        import sys
        import time

        print("ready", file=sys.stderr, flush=True)
        time.sleep(60)
      """)
    shell_script = code(r"""
        set -m
        /bin/sh -c 'sleep 0.1; exec "$1" sandbox -- python -c "$2"' \
          _ "$1" "$2" | \
          /bin/sh -c 'echo "peer:$$" >&2; exec sleep 60'
        printf '__pipeline_done__\n'
        """)
    master, slave, attach = _open_controlling_terminal()
    process = subprocess.Popen(
        ["/bin/sh", "-c", shell_script, "_", binary, sandboxed_script],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        preexec_fn=attach,
    )
    os.close(slave)
    foreground_group = None
    peer_pid = None
    try:
        startup = _read_until(
            master,
            (b"peer:", b"ready\r\n"),
            "both foreground pipeline stages",
        )
        peer_line = next(
            line for line in startup.splitlines() if line.startswith(b"peer:")
        )
        peer_pid = int(peer_line.removeprefix(b"peer:"))
        foreground_group = os.tcgetpgrp(master)
        assert os.getpgid(peer_pid) == foreground_group

        os.write(master, b"\x03")
        _read_until(master, b"__pipeline_done__\r\n", "the interrupted pipeline")
        returncode = process.wait(timeout=5)
        survivors = _kill_survivors([peer_pid])
    except BaseException:
        _kill_process_groups([foreground_group, process.pid])
        if peer_pid is not None:
            _kill_survivors([peer_pif])
        if process.poll() is None:
            process.kill()
        process.wait(timeout=TIMEOUT)
        raise
    finally:
        os.close(master)

    assert returncode == 0, returncode
    assert survivors == [], f"pipeline peer survived: {survivors}"
    return [
        {
            "pipeline": [
                _command("sandbox", "--", "python", "-c", "<script>"),
                ["sleep", "<duration>"],
            ],
            "stdin": "<Ctrl-C>",
            "result": "both pipeline stages exited",
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
