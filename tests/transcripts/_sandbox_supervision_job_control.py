import os
import signal
import subprocess
import sys
from pathlib import Path

from _sandbox_supervision_helpers import (
    TIMEOUT,
    _command,
    _kill_process_groups,
    _open_controlling_terminal,
    _read_until,
    _wait_for_stop,
)
from _support import Transcript, code


def test_stops_and_continues_foreground_sandbox_job(binary: Path) -> Transcript:
    # fmt: python
    sandboxed_script = code(r"""
        import os
        import signal

        terminal = open("/dev/tty", "w")

        def continued(_signal, _frame):
            print("continued", file=terminal, flush=True)

        def interrupted(_signal, _frame):
            raise SystemExit(0)

        signal.signal(signal.SIGCONT, continued)
        signal.signal(signal.SIGINT, interrupted)
        print(f"ready:{os.getpgrp()}", file=terminal, flush=True)
        while True:
            signal.pause()
        """)
    arguments = ("sandbox", "--", "python", "-c", sandboxed_script)
    master, slave, attach = _open_controlling_terminal()
    process = subprocess.Popen(
        [binary, *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(slave,),
        preexec_fn=attach,
    )
    os.close(slave)
    sandbox_group = None
    try:
        ready_line = _read_until(master, b"\n", "sandbox job readiness").decode()
        prefix, group = ready_line.strip().split(":", maxsplit=1)
        assert prefix == "ready", ready_line
        sandbox_group = int(group)
        assert sandbox_group == process.pid
        assert os.tcgetpgrp(master) == sandbox_group

        os.write(master, b"\x1a")
        stop_status = _wait_for_stop(process.pid)
        assert os.WSTOPSIG(stop_status) == signal.SIGTSTP
        assert os.tcgetpgrp(master) == sandbox_group

        os.killpg(sandbox_group, signal.SIGCONT)
        _read_until(master, b"continued\r\n", "continued sandbox job")
        continued = "continued\n"
        os.write(master, b"\x03")
        process.wait(timeout=5)
    except BaseException:
        _kill_process_groups([sandbox_group, process.pid])
        process.wait(timeout=TIMEOUT)
        raise
    finally:
        os.close(master)

    assert process.returncode == 0, process.returncode
    return [
        {
            "command": _command(*arguments),
            "stdout": "ready\n",
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


def test_foregrounds_background_terminal_reader(binary: Path) -> Transcript:
    host_script = code(r"""
        import fcntl
        import os
        import pty
        import select
        import signal
        import subprocess
        import sys
        import termios
        import time

        master, slave = pty.openpty()
        process = None

        def read_until(*markers):
            output = bytearray()
            deadline = time.monotonic() + 5
            while not all(marker in output for marker in markers):
                remaining = deadline - time.monotonic()
                assert remaining > 0, (markers, output)
                readable, _, _ = select.select([master], [], [], remaining)
                assert readable, (markers, output)
                chunk = os.read(master, 4096)
                assert chunk, (markers, output)
                output.extend(chunk)
            return bytes(output)

        try:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            os.tcsetpgrp(slave, os.getpgrp())
            process = subprocess.Popen(
                [sys.argv[1], "sandbox", "--", "python", "-c", sys.argv[2]],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                preexec_fn=os.setpgrp,
            )

            deadline = time.monotonic() + 5
            while True:
                waited, status = os.waitpid(
                    process.pid,
                    os.WUNTRACED | os.WNOHANG,
                )
                if waited == process.pid:
                    break
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert os.WIFSTOPPED(status), status
            assert os.WSTOPSIG(status) == signal.SIGTTIN, status
            assert os.tcgetpgrp(slave) == os.getpgrp()

            os.tcsetpgrp(slave, process.pid)
            os.killpg(process.pid, signal.SIGCONT)
            os.write(master, b"hello\n")
            output = read_until(b"ready:", b"received:hello\r\n")
            ready_line = next(
                line for line in output.splitlines() if line.startswith(b"ready:")
            )
            assert int(ready_line.removeprefix(b"ready:")) == process.pid
            returncode = process.wait(timeout=5)
            assert returncode == 0, returncode
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
            os.close(master)
            os.close(slave)

        print("stopped:SIGTTIN")
        print("foregrounded:shared-group")
        print("received:hello")
        """)
    # fmt: python
    sandboxed_script = code(r"""
        import os

        print(f"ready:{os.getpgrp()}", flush=True)
        line = input()
        print(f"received:{line}", flush=True)
        """)
    result = subprocess.run(
        [sys.executable, "-c", host_script, binary, sandboxed_script],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )

    assert result.returncode == 0, result
    assert result.stdout == (
        "stopped:SIGTTIN\n"
        "foregrounded:shared-group\n"
        "received:hello\n"
    ), result.stdout
    assert result.stderr == "", result.stderr
    return [
        {
            "command": _command("sandbox", "--", "python", "-c", "<script>"),
            "launch": "background terminal job",
            "state": "stopped with SIGTTIN",
        },
        {
            "control": "foreground and SIGCONT",
            "stdin": "hello\n",
            "stdout": "received:hello\n",
            "exit_code": result.returncode,
        },
    ]
