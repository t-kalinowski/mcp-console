#!/usr/bin/env -S uv run --script

import select
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.cli._harness import (
    TIMEOUT,
    _cleanup,
    _command_record,
    _start_lifetime,
    _wait_for_cleanup,
)
from support.macos import (
    signal_darwin_process,
    wait_for_darwin_process_state as _wait_for_process_state,
)
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite


PLATFORMS = {"darwin"}


def test_pending_signal_at_root_exit_preserves_status(binary: Path) -> Transcript:
    lifetime = _start_lifetime(binary)
    exit_events = select.kqueue()
    launcher_resumed = False
    try:
        root_exit = select.kevent(
            lifetime.root[0],
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_EXIT,
        )
        assert exit_events.control([root_exit], 0, 0) == []
        assert signal_darwin_process(lifetime.launcher, signal.SIGSTOP), (
            "sandbox launcher exited before stop injection"
        )
        _wait_for_process_state(lifetime.launcher, "T", "sandbox launcher")

        assert signal_darwin_process(lifetime.launcher, signal.SIGTERM), (
            "sandbox launcher exited before pending-signal injection"
        )
        lifetime.process.stdin.write(b"exit\n")
        lifetime.process.stdin.close()
        events = exit_events.control(None, 1, TIMEOUT)
        assert len(events) == 1, "sandbox root did not exit while launcher was stopped"
        assert events[0].ident == lifetime.root[0], events[0]
        assert events[0].filter == select.KQ_FILTER_PROC, events[0]
        assert events[0].fflags & select.KQ_NOTE_EXIT, events[0]

        assert signal_darwin_process(lifetime.launcher, signal.SIGCONT), (
            "sandbox launcher exited before resume injection"
        )
        launcher_resumed = True
        returncode = lifetime.process.wait(timeout=TIMEOUT)
        stderr = lifetime.process.stderr.read().decode("utf-8")
        survivors = _wait_for_cleanup(lifetime)

        assert returncode == 23, (returncode, stderr)
        assert stderr == "", stderr
        assert survivors == [], f"sandbox processes survived root exit: {survivors}"
        assert not lifetime.temporary_directory.exists(), (
            "pending launcher signal preserved the sandbox temporary directory"
        )
        return [
            _command_record(lifetime),
            {
                "launcher_signal": "SIGSTOP",
                "pending_launcher_signal": "SIGTERM",
                "root_action": "exit 23",
                "verified_pending_signal": "before launcher resume",
            },
            {
                "launcher_signal": "SIGCONT",
                "launcher_returncode": returncode,
                "verified_signal": "consumed without replacing root status",
                "verified_cleanup": (
                    "sandbox root, detached descendant, manager, and temp"
                ),
            },
        ]
    finally:
        if not launcher_resumed:
            signal_darwin_process(lifetime.launcher, signal.SIGCONT)
        exit_events.close()
        _cleanup(lifetime)


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
