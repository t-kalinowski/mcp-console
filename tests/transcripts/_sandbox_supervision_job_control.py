import os
import signal
import subprocess
from pathlib import Path

from _sandbox_supervision_helpers import TIMEOUT, _command, _kill_process_groups, _open_controlling_terminal, _wait_for_stop
from _support import Transcript, code


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
