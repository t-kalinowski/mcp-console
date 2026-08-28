import os
import signal
import subprocess
from pathlib import Path

from _sandbox_supervision_helpers import (
    TIMEOUT,
    _command,
    _kill_process_groups,
    _open_controlling_terminal,
    _read_until,
)
from _support import Transcript, code


def test_preserves_foreground_pipeline_job_control(binary: Path) -> Transcript:
    # The first stage starts without synchronization with its downstream peer.
    # Keeping the sandbox root in the shell-created pipeline group makes that
    # fork order irrelevant and preserves terminal input and terminal signals.
    # fmt: python
    sandboxed_script = code(r"""
        import os
        import signal

        def report(name):
            print(name, flush=True)

        def interrupted(_signal, _frame):
            report("interrupt")
            raise SystemExit(0)

        signal.signal(signal.SIGWINCH, lambda *_: report("winch"))
        signal.signal(signal.SIGINFO, lambda *_: report("info"))
        signal.signal(signal.SIGINT, interrupted)
        print(f"ready:{os.getpgrp()}", flush=True)
        line = input()
        print(f"input:{line}", flush=True)
        while True:
            signal.pause()
        """)
    shell_script = code(r"""
        set -m
        "$1" sandbox -- python -c "$2" | /bin/sh -c 'trap "" INT; exec cat'
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
    pipeline_group = None
    try:
        startup = _read_until(master, b"ready:", "sandbox pipeline readiness")
        sandbox_group = None
        while sandbox_group is None:
            for line in startup.splitlines():
                if line.startswith(b"ready:"):
                    group = line.removeprefix(b"ready:")
                    if group.isdigit():
                        sandbox_group = int(group)
                        break
            if sandbox_group is None:
                startup += _read_until(master, b"\r\n", "sandbox pipeline group")
        pipeline_group = os.tcgetpgrp(master)
        assert sandbox_group == pipeline_group

        os.write(master, b"hello\n")
        _read_until(master, b"input:hello\r\n", "sandbox pipeline input")

        os.killpg(pipeline_group, signal.SIGWINCH)
        _read_until(master, b"winch\r\n", "sandbox pipeline SIGWINCH")
        os.killpg(pipeline_group, signal.SIGINFO)
        _read_until(master, b"info\r\n", "sandbox pipeline SIGINFO")

        os.write(master, b"\x03")
        _read_until(
            master,
            (b"interrupt\r\n", b"__pipeline_done__\r\n"),
            "interrupted sandbox pipeline",
        )
        returncode = process.wait(timeout=5)
    except BaseException:
        _kill_process_groups([pipeline_group, process.pid])
        if process.poll() is None:
            process.kill()
        process.wait(timeout=TIMEOUT)
        raise
    finally:
        os.close(master)

    assert returncode == 0, returncode
    return [
        {
            "pipeline": [
                _command("sandbox", "--", "python", "-c", "<script>"),
                ["cat"],
            ],
            "stdin": "hello\n<SIGWINCH>\n<SIGINFO>\n<Ctrl-C>",
            "stdout": (
                "ready\n"
                "input:hello\n"
                "winch\n"
                "info\n"
                "interrupt\n"
                "__pipeline_done__\n"
            ),
            "exit_code": returncode,
        }
    ]
