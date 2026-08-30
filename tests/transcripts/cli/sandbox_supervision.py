#!/usr/bin/env -S uv run --script

import ctypes
import errno
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
PROC_PIDTBSDINFO = 3
INCLUDE_ZOMBIES = 1


_ProcessIdentity = tuple[int, int, int]


def _command(*arguments: str) -> list[str]:
    return ["mcp-console", *arguments]


class _ProcessInfo(ctypes.Structure):
    # In Darwin's stable proc_bsdinfo ABI, the two start-time fields follow a
    # 120-byte prefix and complete the 136-byte structure.
    _fields_ = [
        ("prefix", ctypes.c_byte * 120),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


_LIBPROC = None
if sys.platform == "darwin":
    _LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    _LIBPROC.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    _LIBPROC.proc_pidinfo.restype = ctypes.c_int


def _process_identity(pid: int) -> _ProcessIdentity | None:
    assert _LIBPROC is not None
    info = _ProcessInfo()
    ctypes.set_errno(0)
    size = _LIBPROC.proc_pidinfo(
        pid,
        PROC_PIDTBSDINFO,
        INCLUDE_ZOMBIES,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if size == ctypes.sizeof(info):
        return (pid, info.pbi_start_tvsec, info.pbi_start_tvusec)
    error = ctypes.get_errno()
    if size == 0 and error == errno.ESRCH:
        return None
    if size == 0 and error != 0:
        raise OSError(error, f"failed to inspect process {pid}")
    raise RuntimeError(
        f"proc_pidinfo returned {size} bytes for process {pid}, "
        f"expected {ctypes.sizeof(info)}"
    )


def _capture_identity(pid: int) -> _ProcessIdentity:
    identity = _process_identity(pid)
    assert identity is not None, f"process {pid} exited before identity capture"
    return identity


def _kill_survivors(identities: list[_ProcessIdentity]) -> list[int]:
    survivors = [
        identity[0]
        for identity in identities
        if _process_identity(identity[0]) == identity
    ]
    for identity in identities:
        # macOS has no pidfd-like signal API. Recheck the start time immediately
        # before cleanup so a reused PID is not treated as the test process.
        if _process_identity(identity[0]) != identity:
            continue
        try:
            os.kill(identity[0], signal.SIGKILL)
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
        writeLines(c(
          as.character(Sys.getpid()),
          as.character(child$get_pid()),
          child_output
        ))
        flush.console()
        stopifnot(identical(readLines("stdin", n = 1L), "exit"))
        quit(save = "no", status = 23L, runLast = FALSE)
        """)
    arguments = ("sandbox", "--", "Rscript", "--vanilla", "-e", script)
    process = subprocess.Popen(
        [binary, *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    identities: list[_ProcessIdentity] = []
    try:
        root_pid, child_pid, grandchild_pid, temporary_directory = _read_lines(
            process.stdout,
            4,
            "the sandbox root, processx descendants, and temporary directory",
        )
        pids = [int(root_pid), int(child_pid), int(grandchild_pid)]
        for pid in pids:
            identities.append(_capture_identity(pid))
        temporary_directory = Path(temporary_directory)

        assert os.getsid(pids[1]) != os.getsid(pids[0])
        assert os.getsid(pids[2]) != os.getsid(pids[1])

        process.stdin.write(b"exit\n")
        process.stdin.close()
        returncode = process.wait(timeout=TIMEOUT)
        stderr = process.stderr.read().decode("utf-8")
        survivors = _kill_survivors(identities[1:])

        assert returncode == 23, returncode
        assert stderr == "", stderr
        assert survivors == [], f"sandbox descendants survived: {survivors}"
        assert not temporary_directory.exists(), (
            f"sandbox temporary directory survived: {temporary_directory}"
        )
    finally:
        if process.poll() is None:
            if not process.stdin.closed:
                try:
                    process.stdin.write(b"exit\n")
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            try:
                process.wait(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=TIMEOUT)
        _kill_survivors(identities)
        if not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()
        process.stderr.close()

    return [
        {
            "command": ["mcp-console", *arguments],
            "exit_code": returncode,
            "stdout": (
                "<sandbox root pid>\n"
                "<processx child pid>\n"
                "<processx grandchild pid>\n"
                "<sandbox temp>\n"
            ),
            "verified_descendants": [
                "processx child outside root session",
                "processx grandchild outside child session",
            ],
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

    identities: list[_ProcessIdentity] = []
    try:
        root_pid, child_pid = [
            int(line)
            for line in _read_lines(
                process.stdout,
                2,
                "the sandbox root and processx child PIDs",
            )
        ]
        identities.append(_capture_identity(root_pid))
        identities.append(_capture_identity(child_pid))
        supervisor_pid = _child_pid_by_name(root_pid, "supervisor")
        identities.append(_capture_identity(supervisor_pid))
        root_group = os.getpgid(root_pid)
        assert os.getpgid(child_pid) != root_group
        assert os.getpgid(supervisor_pid) != root_group

        os.kill(root_pid, signal.SIGKILL)
        returncode = process.wait(timeout=TIMEOUT)
        stderr = process.stderr.read().decode("utf-8")
        survivors = _kill_survivors(identities[1:])

        assert returncode == 137, returncode
        assert stderr == "", stderr
        assert survivors == [], f"processx-supervised processes survived: {survivors}"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT)
        _kill_survivors(identities)
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


def test_relays_interrupt_then_retires_descendants(binary: Path) -> Transcript:
    # fmt: r
    script = code(r"""
        child <- processx::process$new(
          "/bin/sleep",
          "60",
          cleanup = FALSE
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

    identities: list[_ProcessIdentity] = []
    try:
        pids = [
            int(line)
            for line in _read_lines(
                process.stdout,
                2,
                "the sandbox root and processx descendant PIDs",
            )
        ]
        identities = [_capture_identity(pid) for pid in pids]
        os.kill(process.pid, signal.SIGINT)
        returncode = process.wait(timeout=TIMEOUT)
        stderr = process.stderr.read().decode("utf-8")
        survivors = _kill_survivors(identities)

        assert returncode == 130, returncode
        assert stderr == "", stderr
        assert survivors == [], f"interrupted sandbox processes survived: {survivors}"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT)
        _kill_survivors(identities)
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


def test_sandbox_cannot_retain_its_temporary_directory(binary: Path) -> Transcript:
    # fmt: python
    sandboxed_script = code(r"""
        import os
        from pathlib import Path

        temporary_directory = Path(os.environ["TMPDIR"])
        (temporary_directory / ".mcp-console-preserve").write_text("retain\n")
        print(temporary_directory)
        """)
    arguments = ("sandbox", "--", "python", "-c", sandboxed_script)
    result = subprocess.run(
        [binary, *arguments],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )

    temporary_directory = Path(result.stdout.strip())
    assert result.returncode == 0, result
    assert result.stderr == "", result.stderr
    assert not temporary_directory.exists(), (
        f"sandbox retained its host-owned temporary directory: {temporary_directory}"
    )
    return [
        {
            "command": _command(*arguments),
            "stdout": "<sandbox temp>\n",
            "transcript_normalization": {
                "target": "stdout",
                "sandbox_temporary_directory": "omitted",
            },
        }
    ]


def test_delivers_terminal_interrupt_once(binary: Path) -> Transcript:
    host_script = code(r"""
        import fcntl
        import os
        import pty
        import signal
        import subprocess
        import sys
        import termios

        master, slave = pty.openpty()
        sandbox_group = None

        def attach_controlling_terminal():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            os.tcsetpgrp(slave, os.getpid())

        process = subprocess.Popen(
            [sys.argv[1], "sandbox", "--", "python", "-c", sys.argv[2]],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=attach_controlling_terminal,
        )
        os.close(slave)
        try:
            assert process.stdout is not None
            readiness = process.stdout.readline().split()
            assert len(readiness) == 2 and readiness[0] == "ready", readiness
            sandbox_group = int(readiness[1])
            assert os.tcgetpgrp(master) == sandbox_group
            assert sandbox_group != process.pid
            os.write(master, b"sandbox input\n")
            assert process.stdout.readline() == "sandbox input\n"
            assert process.stdout.readline() == "interrupt ready\n"
            os.write(master, b"\x03")
            stdout, stderr = process.communicate(timeout=5)
        except BaseException:
            for process_group in (sandbox_group, process.pid):
                if process_group is None:
                    continue
                try:
                    os.killpg(process_group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            raise
        finally:
            os.close(master)

        sys.stdout.write(stdout)
        sys.stderr.write(stderr)
        raise SystemExit(process.returncode)
        """)
    # fmt: python
    sandboxed_script = code(r"""
        import os
        import signal

        interrupts = 0


        def handle_interrupt(_signal, _frame):
            global interrupts
            interrupts += 1


        signal.signal(signal.SIGINT, handle_interrupt)
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        assert signal.SIGINT not in previous_mask
        print(f"ready {os.getpgrp()}", flush=True)
        print(input(), flush=True)
        print("interrupt ready", flush=True)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        while interrupts == 0:
            signal.pause()
        print(interrupts)
        """)
    result = subprocess.run(
        ["python", "-c", host_script, binary, sandboxed_script],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )

    assert result.returncode == 0, result
    assert result.stdout == "1\n", result.stdout
    assert result.stderr == "", result.stderr
    return [
        {
            "command": _command("sandbox", "--", "python", "-c", sandboxed_script),
            "stdin": "sandbox input\n<Ctrl-C>",
            "stdout": result.stdout,
        }
    ]


def test_preserves_status_after_terminal_closes(binary: Path) -> Transcript:
    host_script = code(r"""
        import ctypes
        import fcntl
        import os
        import pty
        import signal
        import subprocess
        import sys
        import tempfile
        import termios

        master, slave = pty.openpty()
        slave_name = os.ttyname(slave)
        sandbox_group = None

        def attach_controlling_terminal():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            os.tcsetpgrp(slave, os.getpid())

        with tempfile.TemporaryDirectory() as directory:
            release = os.path.join(directory, "release")
            process = subprocess.Popen(
                [
                    sys.argv[1],
                    "sandbox",
                    "--",
                    "python",
                    "-c",
                    sys.argv[2],
                    release,
                ],
                stdin=slave,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                preexec_fn=attach_controlling_terminal,
            )
            os.close(slave)
            try:
                assert process.stdout is not None
                assert process.stdout.readline() == "ready\n"
                sandbox_group = os.tcgetpgrp(master)
                libc = ctypes.CDLL(None, use_errno=True)
                assert libc.revoke(slave_name.encode()) == 0
                os.close(master)
                master = None
                release_descriptor = os.open(
                    release, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                os.close(release_descriptor)
                stdout, stderr = process.communicate(timeout=5)
            except BaseException:
                for process_group in (sandbox_group, process.pid):
                    if process_group is None:
                        continue
                    try:
                        os.killpg(process_group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
                raise
            finally:
                if master is not None:
                    os.close(master)

        sys.stdout.write(stdout)
        sys.stderr.write(stderr)
        raise SystemExit(process.returncode)
        """)
    # fmt: python
    sandboxed_script = code(r"""
        import os
        import signal
        import sys
        import time

        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        print("ready", flush=True)
        while not os.path.exists(sys.argv[1]):
            time.sleep(0.01)
        raise SystemExit(23)
        """)
    result = subprocess.run(
        ["python", "-c", host_script, binary, sandboxed_script],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )

    assert result.returncode == 23, result
    assert result.stdout == "", result.stdout
    assert result.stderr == "", result.stderr
    return [
        {
            "command": _command(
                "sandbox",
                "--",
                "python",
                "-c",
                sandboxed_script,
                "<release gate>",
            ),
            "terminal": "closed after readiness",
            "exit_code": result.returncode,
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
