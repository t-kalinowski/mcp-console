#!/usr/bin/env -S uv run --script

import os
import subprocess
from pathlib import Path

from _support import Transcript, run_this_suite


def record(binary: Path, *arguments: str) -> dict[str, object]:
    result = subprocess.run(
        [binary, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "NO_COLOR": "1"},
    )
    transcript: dict[str, object] = {
        "command": " ".join(("mcp-console", *arguments))
    }
    if result.returncode != 0:
        transcript["exit_code"] = result.returncode
    transcript["stdout"] = result.stdout
    if result.stderr:
        transcript["stderr"] = result.stderr
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
