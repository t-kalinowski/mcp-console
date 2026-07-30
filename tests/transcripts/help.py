#!/usr/bin/env -S uv run --script

import os
import subprocess
from pathlib import Path

from _support import Transcript, TranscriptEntry, run_this_suite


def record(binary: Path, *arguments: str) -> TranscriptEntry:
    result = subprocess.run(
        [binary, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "NO_COLOR": "1"},
    )
    entry: TranscriptEntry = {"command": " ".join(("mcp-console", *arguments))}
    if result.returncode != 0:
        entry["exit_code"] = result.returncode
    entry["stdout"] = result.stdout
    if result.stderr:
        entry["stderr"] = result.stderr
    return entry


def test_help(binary: Path) -> Transcript:
    return [
        record(binary),
        record(binary, "--help"),
        record(binary, "serve", "--help"),
        record(binary, "sandbox"),
        record(binary, "sandbox", "--help"),
    ]


def test_version(binary: Path) -> Transcript:
    return [record(binary, "--version")]


if __name__ == "__main__":
    run_this_suite(__file__)
