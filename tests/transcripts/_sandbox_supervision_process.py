import fcntl
import os
import signal
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from _sandbox_supervision_helpers import TIMEOUT, _command, _kill_survivors, _pid_is_alive, _read_lines
from _sandbox_supervision_helpers import _open_controlling_terminal
from _support import Transcript, code


def test_retires_processx_descendants_across_sessions(binary: Path) -> Transcript:
    # Nested processx generations both call setsid(), leaving the root group.
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
    master, slave, attach = _open_controlling_terminal()
    process = subprocess.Popen(
        [binary, *arguments],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=attach,
    )
    os.close(slave)
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
        os.close(master)

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
