import os
import signal
import subprocess
from pathlib import Path

from _sandbox_supervision_helpers import TIMEOUT, _command, _kill_process_groups, _open_controlling_terminal, _wait_for_stop
from _support import Transcript, code


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
