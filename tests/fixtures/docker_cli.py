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
    stream.write(
        json.dumps({"pid": os.getpid(), "ppid": os.getppid(), "args": arguments}) + "\n"
    )
mode = (root / "mode").read_text() if (root / "mode").exists() else ""

if mode == "stop-oversized":
    # A fake daemon establishes orchestration only, not real container cleanup.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    identity = "a" * 64
    state = root / "containers"
    if arguments == ["context", "inspect"]:
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
    elif "image" in arguments:
        assert arguments[arguments.index("image") + 1] == "inspect"
        print(json.dumps([{"Id": "sha256:" + "b" * 64, "Os": "linux"}]))
    else:
        operation = arguments[arguments.index("container") + 1]
        if operation == "create":
            assert not state.exists()
            state.write_text(identity)
            print(identity)
        elif operation == "start":
            # Consume the full request before failing the target attachment.
            length = int.from_bytes(sys.stdin.buffer.read(4), "big")
            assert len(sys.stdin.buffer.read(length)) == length
            sys.exit("fixture: target attachment failed")
        elif operation == "stop":
            assert arguments[-1] == state.read_text()
            sys.stdout.buffer.write(b"x" * (1024 * 1024 + 1))
            sys.stdout.buffer.flush()
            signal.pause()  # The command owner must abort this CLI on overflow.
        elif operation == "rm":
            assert arguments[-1] == state.read_text()
            state.unlink()
            print(identity)
        else:
            assert operation == "ls"
            if state.exists():
                print(state.read_text())
    raise SystemExit(0)

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
