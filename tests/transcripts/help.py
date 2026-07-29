#!/usr/bin/env -S uv run --script

import re
import subprocess
from pathlib import Path

from _support import Transcript, run_this_suite


ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def record(binary: Path, *arguments: str) -> dict[str, object]:
    result = subprocess.run(
        [binary, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    transcript: dict[str, object] = {
        "command": " ".join(("mcp-console", *arguments))
    }
    if result.returncode != 0:
        transcript["exit_code"] = result.returncode
    transcript["stdout"] = ANSI_ESCAPE.sub("", result.stdout)
    if stderr := ANSI_ESCAPE.sub("", result.stderr):
        transcript["stderr"] = stderr
    return transcript


def test_help(binary: Path) -> Transcript:
    return [
        record(binary),
        record(binary, "--help"),
        record(binary, "serve", "--help"),
        record(binary, "sandbox"),
        record(binary, "sandbox", "--help"),
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
