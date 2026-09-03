#!/usr/bin/env -S uv run --script

import ctypes
import errno
import fcntl
import os
import pty
import selectors
import signal
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import Transcript, code, run_this_suite


PLATFORMS = {"darwin"}
TIMEOUT = 10
PROC_PIDTBSDINFO = 3
INCLUDE_ZOMBIES = 1


_ProcessIdentity = tuple[int, int, int]


def _command(*arguments: str) -> list[str]:
    return ["mcp-console", *arguments]


def _start_with_controlling_terminal(
    arguments: list[str | Path],
) -> tuple[subprocess.Popen[bytes], int, str]:
    master, slave = pty.openpty()
    slave_name = os.ttyname(slave)

    def attach_controlling_terminal() -> None:
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        os.tcsetpgrp(slave, os.getpid())

    process = subprocess.Popen(
        arguments,
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=attach_controlling_terminal,
    )
    os.close(slave)
    assert process.stdout is not None
    assert process.stderr is not None
    return process, master, slave_name


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
    # The processx child starts a new session on Unix. Its lightweight Python
    # program starts the sleep grandchild in a third session, so neither
    # descendant remains in its parent's process group or session.
    # fmt: r
    script = code(r"""
        child_script <- '
        import os
        import subprocess
        import time

        grandchild = subprocess.Popen(
            ["/bin/sleep", "60"],
            start_new_session=True,
        )
        payload = (
            str(grandchild.pid)
            + os.linesep
            + os.environ["TMPDIR"]
            + os.linesep
        ).encode()
        os.write(1, payload)
        time.sleep(60)
        '

        child <- processx::process$new(
          "python",
          c("-c", child_script),
          stdout = "|",
          stderr = "2>&1",
          cleanup = FALSE
        )
        stopifnot(child$poll_io(-1)[["output"]] == "ready")
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
                "detached grandchild outside processx child session",
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
        tryCatch(
          {
            writeLines(c(
              as.character(Sys.getpid()),
              as.character(child$get_pid())
            ))
            flush.console()
            Sys.sleep(60)
          },
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
        assert os.getpgid(pids[0]) == pids[0]
        assert os.getpgid(pids[1]) != os.getpgid(pids[0])
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
            "verified_descendant": "processx child outside root process group",
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
    # fmt: python
    sandboxed_script = code(r"""
        import os
        import signal
        import threading

        interrupts = 0
        interrupted = threading.Event()


        def handle_interrupt(_signal, _frame):
            global interrupts
            interrupts += 1
            interrupted.set()


        signal.signal(signal.SIGINT, handle_interrupt)
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        assert signal.SIGINT not in previous_mask
        print(f"ready {os.getpid()} {os.getpgrp()}", flush=True)
        print(input(), flush=True)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        print("interrupt ready", flush=True)
        interrupted.wait()
        print(interrupts)
        """)
    arguments = [binary, "sandbox", "--", "python", "-c", sandboxed_script]
    process, master, _ = _start_with_controlling_terminal(arguments)
    identities: list[_ProcessIdentity] = []
    try:
        readiness = _read_lines(
            process.stdout,
            1,
            "the foreground sandbox readiness",
        )[0].split()
        assert len(readiness) == 3 and readiness[0] == "ready", readiness
        target_pid, target_group = map(int, readiness[1:])
        identities.append(_capture_identity(target_pid))
        assert target_group == target_pid
        assert target_group != process.pid
        assert os.tcgetpgrp(master) == target_group

        os.write(master, b"sandbox input\n")
        assert _read_lines(process.stdout, 2, "the sandbox input acknowledgement") == [
            "sandbox input",
            "interrupt ready",
        ]
        assert os.tcgetpgrp(master) == target_group
        terminal_attributes = termios.tcgetattr(master)
        assert terminal_attributes[3] & termios.ISIG
        assert terminal_attributes[6][termios.VINTR] == b"\x03"
        os.write(master, b"\x03")
        stdout, stderr = process.communicate(timeout=TIMEOUT)
        survivors = _kill_survivors(identities)

        assert process.returncode == 0, process.returncode
        assert stdout == b"1\n", stdout
        assert stderr == b"", stderr
        assert survivors == [], f"terminal sandbox survived: {survivors}"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT)
        _kill_survivors(identities)
        process.stdout.close()
        process.stderr.close()
        os.close(master)

    return [
        {
            "command": _command("sandbox", "--", "python", "-c", sandboxed_script),
            "stdin": "sandbox input\n<Ctrl-C>",
            "stdout": stdout.decode("utf-8"),
            "terminal_ownership": "transferred to target",
        }
    ]


def test_preserves_terminal_ownership_with_foreground_peer(binary: Path) -> Transcript:
    # A foreground shell pipeline places all of its stages in one process group.
    # Model another stage with a sibling that remains in the launcher's group.
    # fmt: python
    wrapper_script = code(r"""
        import os
        import sys

        peer = os.fork()
        if peer == 0:
            os.closerange(0, 3)
            os.execl("/bin/sleep", "sleep", "60")
        os.execv(
            sys.argv[1],
            [
                sys.argv[1],
                "sandbox",
                "--",
                "python",
                "-c",
                sys.argv[2],
                sys.argv[3],
                str(peer),
            ],
        )
        """)
    # fmt: python
    sandboxed_script = code(r"""
        import os
        import sys

        print(
            f"ready {os.getpid()} {os.getpgrp()} {sys.argv[2]}",
            flush=True,
        )
        with open(sys.argv[1], encoding="utf-8") as release:
            assert release.readline() == "release\n"
        """)

    with tempfile.TemporaryDirectory() as directory:
        release = os.path.join(directory, "release")
        os.mkfifo(release)
        process, master, _ = _start_with_controlling_terminal(
            [sys.executable, "-c", wrapper_script, binary, sandboxed_script, release]
        )

        identities: list[_ProcessIdentity] = []
        release_descriptor = None
        try:
            readiness = _read_lines(
                process.stdout,
                1,
                "the foreground-peer sandbox readiness",
            )[0].split()
            assert len(readiness) == 4 and readiness[0] == "ready", readiness
            target_pid, target_group, peer_pid = map(int, readiness[1:])
            foreground_group = os.tcgetpgrp(master)
            identities.append(_capture_identity(target_pid))
            peer_identity = _capture_identity(peer_pid)
            identities.append(peer_identity)

            assert os.getpgid(process.pid) == process.pid
            assert os.getpgid(peer_pid) == process.pid
            assert target_group == target_pid
            assert foreground_group == process.pid
            assert target_group != foreground_group

            assert _kill_survivors([peer_identity]) == [peer_pid]
            identities.pop()
            release_descriptor = os.open(release, os.O_RDWR | os.O_NONBLOCK)
            os.write(release_descriptor, b"release\n")
            returncode = process.wait(timeout=TIMEOUT)
            stderr = process.stderr.read().decode("utf-8")
            survivors = _kill_survivors(identities)

            assert returncode == 0, returncode
            assert stderr == "", stderr
            assert survivors == [], f"foreground-peer sandbox survived: {survivors}"
        finally:
            if release_descriptor is not None:
                os.close(release_descriptor)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=TIMEOUT)
            _kill_survivors(identities)
            process.stdout.close()
            process.stderr.close()
            os.close(master)

    return [
        {
            "command": _command(
                "sandbox",
                "--",
                "python",
                "-c",
                sandboxed_script,
                "<release gate>",
                "<peer pid>",
            ),
            "foreground_peer": "shares launcher process group",
            "target_process_group": "dedicated",
            "terminal_foreground_group": "launcher and peer",
            "exit_code": returncode,
        }
    ]


def test_preserves_status_after_terminal_closes(binary: Path) -> Transcript:
    # fmt: python
    sandboxed_script = code(r"""
        import os
        import signal
        import sys

        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        print("ready", flush=True)
        with open(sys.argv[1], encoding="utf-8") as release:
            assert release.readline() == "release\n"
        raise SystemExit(23)
        """)
    with tempfile.TemporaryDirectory() as directory:
        release = os.path.join(directory, "release")
        os.mkfifo(release)
        arguments = [
            binary,
            "sandbox",
            "--",
            "python",
            "-c",
            sandboxed_script,
            release,
        ]
        process, master, slave_name = _start_with_controlling_terminal(arguments)
        identities: list[_ProcessIdentity] = []
        release_descriptor = None
        try:
            assert _read_lines(process.stdout, 1, "the terminal-close readiness") == [
                "ready"
            ]
            target_pid = os.tcgetpgrp(master)
            identities.append(_capture_identity(target_pid))

            libc = ctypes.CDLL(None, use_errno=True)
            assert libc.revoke(slave_name.encode()) == 0
            os.close(master)
            master = None
            release_descriptor = os.open(release, os.O_RDWR | os.O_NONBLOCK)
            os.write(release_descriptor, b"release\n")
            stdout, stderr = process.communicate(timeout=TIMEOUT)
            survivors = _kill_survivors(identities)

            assert process.returncode == 23, process.returncode
            assert stdout == b"", stdout
            assert stderr == b"", stderr
            assert survivors == [], f"terminal-close sandbox survived: {survivors}"
        finally:
            if release_descriptor is not None:
                os.close(release_descriptor)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=TIMEOUT)
            _kill_survivors(identities)
            process.stdout.close()
            process.stderr.close()
            if master is not None:
                os.close(master)

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
            "exit_code": process.returncode,
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
