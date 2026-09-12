"""Forward real Docker calls; expose causal gates and selected transport faults."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

arguments = sys.argv[1:]
root = Path(os.environ["CONSOLE_DOCKER_PEER"])
with (root / "calls").open("a") as stream:
    stream.write(json.dumps({"pid": os.getpid(), "args": arguments}) + "\n")
mode = (root / "mode").read_text() if (root / "mode").exists() else ""

if mode == "registry-peer" and "pull" in arguments:
    # A deterministic registry response still resolves and launches a real image.
    result = subprocess.run(
        [
            os.environ["CONSOLE_REAL_DOCKER"],
            *arguments[: arguments.index("image")],
            "image",
            "tag",
            (root / "source-image").read_text(),
            arguments[-1],
        ]
    )
    print("registry peer: pulled image", flush=True)
    raise SystemExit(result.returncode)

if mode == "setup-gate" and "context" in arguments:
    print(
        json.dumps(
            [
                {
                    "Name": "default",
                    "Endpoints": {"docker": {"Host": "unix:///unused-docker-peer"}},
                }
            ]
        )
    )
    raise SystemExit(0)

if mode == "setup-gate" and "pull" in arguments:
    with (root / "reached").open("wb", buffering=0) as stream:
        stream.write(b"1")
    signal.pause()
    raise SystemExit(91)

if mode == "cleanup-hang" and any(
    operation in arguments for operation in ("stop", "rm", "ls")
):
    signal.pause()
    raise SystemExit(94)

if mode == "cleanup-loss" and any(
    operation in arguments for operation in ("stop", "rm", "ls")
):
    print("fixture: Docker daemon communication failed", file=sys.stderr)
    raise SystemExit(1)

if mode == "create-unacknowledged" and "create" in arguments:
    with (root / "reached").open("wb", buffering=0) as stream:
        stream.write(b"1")
    signal.pause()
    raise SystemExit(95)

if mode == "create-gate" and "create" in arguments:
    result = subprocess.run(
        [os.environ["CONSOLE_REAL_DOCKER"], *arguments], capture_output=True
    )
    assert result.returncode == 0, result.stderr
    (root / "created").write_bytes(result.stdout)
    with (root / "reached").open("wb", buffering=0) as stream:
        stream.write(b"1")
    signal.pause()
    raise SystemExit(92)

os.execv(
    os.environ["CONSOLE_REAL_DOCKER"], [os.environ["CONSOLE_REAL_DOCKER"], *arguments]
)
