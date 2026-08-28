import os
import signal
import subprocess
from pathlib import Path

from _sandbox_supervision_helpers import TIMEOUT, _command, _kill_process_groups, _kill_survivors, _open_controlling_terminal, _read_until
from _support import Transcript, code


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
