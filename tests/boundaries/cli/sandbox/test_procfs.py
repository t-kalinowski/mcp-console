#!/usr/bin/env -S uv run --script

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.linux_sandbox import inherited_procfs_command
from support.records import Transcript
from support.requirements import FRESH_PROCFS, NESTED_PROCFS, requires
from support.suites import run_this_suite


def probe(binary: Path, procfs: str) -> Transcript:
    fixture = Path(__file__).resolve().parents[3] / "fixtures/cli/sandbox/procfs.py"
    command = [sys.executable, str(fixture), "run", str(binary), "console", procfs]
    if procfs == "inherited":
        command = inherited_procfs_command(command)
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result
    assert result.stderr == "", result
    return [json.loads(line) for line in result.stdout.splitlines()]


@requires(FRESH_PROCFS)
def test_fresh_procfs_preserves_policy_and_supervisor_isolation(
    binary: Path,
) -> Transcript:
    return probe(binary, "fresh")


@requires(NESTED_PROCFS)
def test_inherited_procfs_preserves_policy_and_supervisor_isolation(
    binary: Path,
) -> Transcript:
    return probe(binary, "inherited")


if __name__ == "__main__":
    run_this_suite(__file__)
