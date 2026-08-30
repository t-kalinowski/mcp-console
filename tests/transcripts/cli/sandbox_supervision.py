#!/usr/bin/env -S uv run --script

import os
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import Transcript, code, run_this_suite


PLATFORMS = {"darwin"}
TIMEOUT = 10


def test_preserves_status_when_sigchld_was_ignored(binary: Path) -> Transcript:
    # POSIX exec preserves ignored signal dispositions. Exercise the real binary
    # entry point rather than setting SIGCHLD after MCP Console has initialized.
    # fmt: python
    host_script = code(r"""
        import os
        import signal
        import sys

        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        os.execv(
            sys.argv[1],
            [
                sys.argv[1],
                "sandbox",
                "--",
                "python",
                "-c",
                "raise SystemExit(23)",
            ],
        )
        """)
    result = subprocess.run(
        ["python", "-c", host_script, binary],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )

    assert result.returncode == 23, result
    assert result.stdout == "", result.stdout
    assert result.stderr == "", result.stderr
    return [
        {
            "command": [
                "mcp-console",
                "sandbox",
                "--",
                "python",
                "-c",
                "raise SystemExit(23)",
            ],
            "inherited_sigchld": "ignored",
            "exit_code": result.returncode,
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
