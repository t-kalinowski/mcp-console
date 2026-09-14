"""Deterministic local provider peer: public create/exec/rm/ls, no VM claims."""

import json
import os
import signal
import struct
import subprocess
import sys
import uuid
from pathlib import Path

root = Path(os.environ["CONSOLE_SBX_PEER"])
args = sys.argv[1:]
with (root / "calls").open("a") as stream:
    stream.write(
        json.dumps({"args": args, "pid": os.getpid(), "ppid": os.getppid()}) + "\n"
    )
mode = (root / "mode").read_text() if (root / "mode").exists() else ""
state = root / "vms"


def gate() -> None:
    with (root / "reached").open("wb", buffering=0) as stream:
        stream.write(b"1")
    signal.pause()


real = os.environ.get("CONSOLE_SBX_REAL")
if mode == "diagnostics" and args[0] in ("version", "ls", "create", "rm"):
    print(f"fixture: {args[0]} diagnostic", file=sys.stderr)
    if args[0] in ("create", "rm"):
        print(f"fixture: {args[0]} progress")
if real:
    if args[0] == "ls" and mode == "cleanup-loss":
        sys.exit("fixture: daemon unavailable")
    if args[0] == "ls" and mode == "cleanup-hang":
        signal.pause()
    if args[0] == "create" and mode == "create-unacknowledged":
        gate()
    if args[0] == "create" and mode == "create-gate":
        subprocess.run([real, *args], check=True)
        gate()
    if args[0] == "exec" and (
        (args[-1] == "docker-sandbox-probe" and mode == "probe-gate")
        or (args[-1] == "docker-sandbox-launch" and mode == "launch-gate")
    ):
        gate()
    os.execv(real, [real, *args])


if args == ["version"]:
    print(
        "sbx version: v0.41.0 fixture"
        if mode == "unsupported-version"
        else "sbx version: v0.42.1 fixture"
    )
elif args[0] == "ls":
    if mode == "malformed-listing":
        print('{"sandboxes": [{"name": "missing-id"}]}')
        sys.exit(0)
    if mode == "cleanup-hang" and state.exists():
        gate()
    if mode == "cleanup-loss" and state.exists():
        sys.exit("fixture: daemon unavailable")
    print(
        json.dumps(
            {"sandboxes": json.loads(state.read_text()) if state.exists() else []}
        )
    )
elif args[0] == "create":
    name = args[args.index("--name") + 1]
    if mode == "create-unacknowledged":
        gate()
    if mode == "create-failed":
        sys.exit("fixture: create rejected")
    mounts = args[args.index("shell") + 1 :]
    current = json.loads(state.read_text()) if state.exists() else []
    current.append(
        {
            "name": name,
            "id": str(uuid.uuid4()),
            "agent": "shell",
            "status": "running",
            **({"workspaces": mounts} if mounts else {}),
        }
    )
    state.write_text(json.dumps(current))
    if mode == "create-gate":
        gate()
elif args[0] == "rm":
    assert args[1] == "--force"
    if mode == "removal-failed":
        sys.exit("fixture: removal failed")
    remaining = [vm for vm in json.loads(state.read_text()) if vm["name"] != args[2]]
    if remaining:
        state.write_text(json.dumps(remaining))
    else:
        state.unlink()
elif args[0] == "exec":
    assert args[1:3] == ["-i", "--workdir"] and "-t" not in args
    source = sys.stdin.buffer
    length = struct.unpack(">I", source.read(4))[0]
    bootstrap = json.loads(source.read(length))
    assert args[3] == bootstrap["workspace"]
    assert bootstrap["provider"] == "compute"
    assert set(bootstrap["policy"]) <= {"environment", "inherit_environment"}

    def frame(tag: int, value: dict) -> None:
        payload = json.dumps(value).encode() + (b"\n" if tag == 2 else b"")
        sys.stdout.buffer.write(struct.pack(">BI", tag, len(payload)) + payload)
        sys.stdout.buffer.flush()

    probe = args[-1] == "docker-sandbox-probe"
    if (probe and mode == "probe-gate") or (not probe and mode == "launch-gate"):
        gate()
    frame(1, {"version": bootstrap["version"], "build": bootstrap["build"]})
    if probe:
        frame(2, {"r_home": "/prepared/R", "python": "/prepared/python"})
        frame(3, {"confirmed": True, "error": None})
    else:
        frame(2, {"kind": "ready"})
        for line in source:
            command = json.loads(line)
            if command["kind"] == "evaluate":
                frame(2, {"kind": "console_output", "data": "provider peer\n"})
                frame(2, {"kind": "completed"})
            elif command["kind"] == "shutdown":
                frame(2, {"kind": "shutdown_started"})
                frame(2, {"kind": "worker_exited", "code": 0})
                frame(3, {"confirmed": True, "error": None})
                break
else:
    raise AssertionError(args)
