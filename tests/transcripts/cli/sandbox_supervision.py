#!/usr/bin/env -S uv run --script

import ctypes
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


def _wait_for_stop(process_id: int) -> None:
    deadline = time.monotonic() + TIMEOUT
    while True:
        waited, status = os.waitpid(process_id, os.WUNTRACED | os.WNOHANG)
        if waited == process_id:
            assert os.WIFSTOPPED(status), status
            return
        assert time.monotonic() < deadline, "timed out waiting for launcher stop"
        time.sleep(0.01)


def _build_tracker_start_failure_interposer(directory: Path) -> Path:
    source = directory / "fail-tracker-start.c"
    library = directory / "fail-tracker-start.dylib"
    source.write_text(
        r"""
#include <errno.h>
#include <semaphore.h>
#include <stdlib.h>
#include <sys/event.h>
#include <sys/syscall.h>
#include <unistd.h>

static int process_watch_gated = 0;
static int signal_watch_failed = 0;

static int waits_for_sandbox_descendant(void) {
    const char *name = getenv("MCP_CONSOLE_TEST_TRACKER_SEMAPHORE");
    if (name == NULL) {
        errno = EINVAL;
        return -1;
    }

    sem_t *semaphore = sem_open(name, 0);
    if (semaphore == SEM_FAILED) {
        return -1;
    }
    while (sem_wait(semaphore) != 0) {
        if (errno != EINTR) {
            int error = errno;
            sem_close(semaphore);
            errno = error;
            return -1;
        }
    }
    return sem_close(semaphore);
}

static int fail_tracker_signal_watch(
    int queue,
    const struct kevent *changes,
    int change_count,
    struct kevent *events,
    int event_count,
    const struct timespec *timeout
) {
    for (int index = 0; index < change_count; index++) {
        if (!process_watch_gated && changes[index].filter == EVFILT_PROC) {
            if (waits_for_sandbox_descendant() != 0) {
                return -1;
            }
            process_watch_gated = 1;
        }
    }

    for (int index = 0; index < change_count; index++) {
        if (!signal_watch_failed && changes[index].filter == EVFILT_SIGNAL) {
            signal_watch_failed = 1;
            errno = EIO;
            return -1;
        }
    }

    return (int)syscall(
        SYS_kevent,
        queue,
        changes,
        change_count,
        events,
        event_count,
        timeout
    );
}

__attribute__((constructor))
static void remove_interposer_from_child_environment(void) {
    unsetenv("DYLD_INSERT_LIBRARIES");
}

__attribute__((used))
static struct {
    const void *replacement;
    const void *replacee;
} interposers[] __attribute__((section("__DATA,__interpose"))) = {
    {(const void *)&fail_tracker_signal_watch, (const void *)&kevent},
};
""".removeprefix("\n"),
        encoding="utf-8",
    )
    subprocess.run(
        ["cc", "-dynamiclib", "-o", library, source],
        check=True,
        capture_output=True,
        text=True,
    )
    return library


def _create_semaphore(name: bytes) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sem_open.restype = ctypes.c_void_p
    libc.sem_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
    libc.sem_close.argtypes = [ctypes.c_void_p]
    semaphore = libc.sem_open(
        name,
        os.O_CREAT | os.O_EXCL,
        ctypes.c_uint(0o600),
        ctypes.c_uint(0),
    )
    assert semaphore != ctypes.c_void_p(-1).value, os.strerror(ctypes.get_errno())
    assert libc.sem_close(semaphore) == 0, os.strerror(ctypes.get_errno())


def _unlink_semaphore(name: bytes) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sem_unlink.argtypes = [ctypes.c_char_p]
    result = libc.sem_unlink(name)
    assert result == 0, os.strerror(ctypes.get_errno())


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
    process = subprocess.Popen(
        [binary, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    pids: list[int] = []
    try:
        lines = _read_lines(
            process.stdout,
            3,
            "the processx descendant PIDs and sandbox temporary directory",
        )
        pids = [int(lines[0]), int(lines[1])]
        temporary_directory = Path(lines[2])
        returncode = process.wait(timeout=TIMEOUT)
        stderr = process.stderr.read().decode("utf-8")
        survivors = _kill_survivors(pids)

        assert returncode == 23, returncode
        assert stderr == "", stderr
        assert survivors == [], f"sandbox descendants survived: {survivors}"
        assert not temporary_directory.exists(), (
            f"sandbox temporary directory survived: {temporary_directory}"
        )
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
            "exit_code": returncode,
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
    process = subprocess.Popen(
        [binary, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    pids: list[int] = []
    try:
        pid = int(_read_lines(process.stdout, 1, "the same-group child PID")[0])
        pids = [pid]
        returncode = process.wait(timeout=TIMEOUT)
        stderr = process.stderr.read().decode("utf-8")
        deadline = time.monotonic() + 1
        while _pid_is_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        survivors = _kill_survivors(pids)

        assert returncode == 0, returncode
        assert stderr == "", stderr
        assert survivors == [], f"same-group sandbox child survived: {survivors}"
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
            "stdout": "<same-group child pid>\n",
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
            "command": _command(*arguments),
            "exit_code": returncode,
            "stdout": "<sandbox root pid>\n<processx-supervised child pid>\n",
            "verified_descendants": [
                "processx child outside root process group",
                "processx crash supervisor outside root process group",
            ],
        }
    ]


def test_retires_observed_descendant_when_tracker_setup_fails(
    binary: Path,
) -> Transcript:
    # The interposer blocks the first process watch until the sandbox root has
    # created a session-changing child. Tracker setup then observes both
    # identities before the injected signal-filter registration failure.
    # fmt: python
    script = code(r"""
        import ctypes
        import os
        import signal

        libc = ctypes.CDLL(None, use_errno=True)
        libc.sem_open.restype = ctypes.c_void_p
        libc.sem_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
        libc.sem_post.argtypes = [ctypes.c_void_p]
        semaphore = libc.sem_open(
            os.environ["MCP_CONSOLE_TEST_TRACKER_SEMAPHORE"].encode(),
            0,
        )
        assert semaphore != ctypes.c_void_p(-1).value

        child = os.fork()
        if child == 0:
            os.setsid()
            print(os.getpid(), flush=True)
            os.close(1)
            os.close(2)
            assert libc.sem_post(semaphore) == 0
            while True:
                signal.pause()

        while True:
            signal.pause()
        """)
    arguments = ("sandbox", "--", "python", "-c", script)

    with TemporaryDirectory() as directory:
        temporary = Path(directory)
        interposer = _build_tracker_start_failure_interposer(temporary)
        semaphore_name = f"/m{os.getpid()}".encode()
        _create_semaphore(semaphore_name)
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(interposer)
        environment["MCP_CONSOLE_TEST_TRACKER_SEMAPHORE"] = semaphore_name.decode()
        try:
            process = subprocess.Popen(
                [binary, *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            assert process.stdout is not None
            assert process.stderr is not None
            pid = None
            try:
                pid = int(
                    _read_lines(
                        process.stdout,
                        1,
                        "the observed sandbox descendant PID",
                    )[0]
                )
                returncode = process.wait(timeout=TIMEOUT)
                stderr = process.stderr.read().decode("utf-8")
                survivors = _kill_survivors([pid])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=TIMEOUT)
                if pid is not None:
                    _kill_survivors([pid])
                process.stdout.close()
                process.stderr.close()
        finally:
            _unlink_semaphore(semaphore_name)

    assert returncode == 1, returncode
    assert stderr == (
        "failed to watch sandbox launcher signals: Input/output error (os error 5)\n"
    ), stderr
    assert survivors == [], f"observed sandbox descendant survived: {survivors}"
    return [
        {
            "command": _command(*arguments),
            "tracker_setup": "signal watch fails after descendant observation",
            "exit_code": returncode,
            "stdout": "<observed descendant pid>\n",
            "stderr": stderr,
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
            assert process.stdout.readline() == "ready\n"
            sandbox_group = os.tcgetpgrp(master)
            os.write(master, b"sandbox input\n")
            assert process.stdout.readline() == "sandbox input\n"
            os.write(master, b"\x03")
            assert process.stdout.readline() == "interrupted\n"
            os.write(master, b"report count\n")
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
        import signal

        interrupts = 0


        def handle_interrupt(_signal, _frame):
            global interrupts
            interrupts += 1
            print("interrupted", flush=True)


        signal.signal(signal.SIGINT, handle_interrupt)
        print("ready", flush=True)
        print(input(), flush=True)
        while interrupts == 0:
            signal.pause()
        input()
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
            "stdin": "sandbox input\n<Ctrl-C>\nreport count\n",
            "stdout": result.stdout,
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
        signal.signal(signal.SIGINT, interrupted)
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
    # The FIFO keeps the first stage from entering MCP Console until its pipeline
    # peer exists in the shell-assigned foreground process group.
    # fmt: python
    sandboxed_script = code(r"""
        import sys
        import time

        print("ready", file=sys.stderr, flush=True)
        time.sleep(60)
        """)
    shell_script = code(r"""
        set -m
        /bin/sh -c 'read -r gate < "$3"; echo "launcher:$$" >&2; exec "$1" sandbox -- python -c "$2"' \
          _ "$1" "$2" "$3" | \
          /bin/sh -c 'printf "ready\n" > "$1"; echo "peer:$$" >&2; exec sleep 60' \
          _ "$3"
        printf '__pipeline_done__\n'
        """)

    with TemporaryDirectory() as directory:
        gate = Path(directory) / "pipeline-ready"
        os.mkfifo(gate)
        master, slave, attach = _open_controlling_terminal()
        process = subprocess.Popen(
            ["/bin/sh", "-c", shell_script, "_", binary, sandboxed_script, gate],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            preexec_fn=attach,
        )
        os.close(slave)
        terminal_group = None
        pipeline_group = None
        peer_pid = None
        try:
            startup = _read_until(
                master,
                (b"peer:", b"launcher:", b"ready\r\n"),
                "both foreground pipeline stages",
            )
            peer_line = next(
                line for line in startup.splitlines() if line.startswith(b"peer:")
            )
            peer_pid = int(peer_line.removeprefix(b"peer:"))
            launcher_line = next(
                line for line in startup.splitlines() if line.startswith(b"launcher:")
            )
            launcher_pid = int(launcher_line.removeprefix(b"launcher:"))
            pipeline_group = os.getpgid(peer_pid)
            assert os.getpgid(launcher_pid) == pipeline_group
            terminal_group = os.tcgetpgrp(master)
            assert terminal_group == pipeline_group

            os.write(master, b"\x03")
            _read_until(master, b"__pipeline_done__\r\n", "the interrupted pipeline")
            returncode = process.wait(timeout=5)
            survivors = _kill_survivors([peer_pid])
        except BaseException:
            _kill_process_groups([terminal_group, pipeline_group, process.pid])
            if peer_pid is not None:
                _kill_survivors([peer_pid])
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


def test_preserves_status_after_terminal_closes(binary: Path) -> Transcript:
    host_script = code(r"""
        import ctypes
        import fcntl
        import os
        import pty
        import signal
        import subprocess
        import sys
        import termios

        master, slave = pty.openpty()
        slave_name = os.ttyname(slave)
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
            assert process.stdout.readline() == "ready\n"
            sandbox_group = os.tcgetpgrp(master)
            libc = ctypes.CDLL(None, use_errno=True)
            assert libc.revoke(slave_name.encode()) == 0
            os.close(master)
            master = None
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

        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        print("ready", flush=True)
        try:
            os.read(0, 1)
        except OSError:
            pass
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
            "command": _command("sandbox", "--", "python", "-c", sandboxed_script),
            "terminal": "closed after readiness",
            "exit_code": result.returncode,
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
