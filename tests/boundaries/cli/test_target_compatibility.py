"""Reject previous target schemas even when the Console package version matches."""

import json
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript
from support.requirements import REMOTE_CONTROLLERS, WORKER, requires
from support.ssh import bootstrap
from support.suites import run_this_suite


@requires(REMOTE_CONTROLLERS)
def test_rejects_previous_launch_schemas(binary: Path) -> Transcript:
    records = []
    for command, previous, current in (
        ("ssh-launch", 10, 11),
        ("docker-launch", 11, 12),
        ("docker-sandbox-launch", 11, 12),
        ("docker-probe", 11, 12),
        ("docker-sandbox-probe", 11, 12),
    ):
        # Version rejection must precede workspace validation or provider setup.
        # The helper supplies the executable's own package version.
        result = subprocess.run(
            [binary, command],
            input=bootstrap(
                binary, Path("relative"), version=previous, sql={}, no_sandbox=True
            ),
            capture_output=True,
            timeout=10,
        )
        error = result.stderr.decode().strip()
        assert result.returncode != 0, result
        assert f"expected protocol {current}" in error, error
        assert f"received protocol {previous}" in error, error
        # A rejected launch confirms no worker exists, without a launch hello.
        assert struct.unpack(">BI", result.stdout[:5]) == (3, len(result.stdout) - 5)
        assert json.loads(result.stdout[5:]) == {"confirmed": True, "error": error}
        records.append({"command": command, "error": error})
    return records


@requires(WORKER)
def test_rejects_previous_local_preparation_schema(binary: Path) -> Transcript:
    return reject_previous_preparation(binary, "resolve", framed=False)


@requires(REMOTE_CONTROLLERS)
def test_rejects_previous_ssh_preparation_schema(binary: Path) -> Transcript:
    return reject_previous_preparation(binary, "ssh-prepare", framed=True)


def reject_previous_preparation(
    binary: Path, command: str, *, framed: bool
) -> Transcript:
    build = subprocess.check_output([binary, "--version"], text=True).split()[1]
    opened = {
        "Open": {
            "version": 6,
            "build": build,
            "workspace": "relative",
            "selections": {},
            "mode": "PythonOnly",
        }
    }
    payload = json.dumps(opened).encode()
    request = struct.pack(">I", len(payload)) + payload if framed else payload + b"\n"
    result = subprocess.run(
        [binary, command], input=request, capture_output=True, timeout=10
    )
    error = result.stderr.decode().strip()
    assert result.returncode != 0, result
    assert error == "incompatible SSH preparation protocol or Console build", error
    assert not result.stdout, result.stdout
    return [{"command": command, "error": error}]


if __name__ == "__main__":
    run_this_suite(__file__)
