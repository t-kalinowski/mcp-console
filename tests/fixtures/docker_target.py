"""Target-side protocol gates, launched in real owned containers."""

import json
import os
import signal
import struct
import sys

mode, operation = sys.argv[1:]
if (mode, operation) in (
    ("probe-gate", "docker-probe"),
    ("launch-gate", "docker-launch"),
):
    print("target launch gate", file=sys.stderr, flush=True)
    signal.pause()
    raise SystemExit(91)

if operation == "docker-probe":
    os.execv("/opt/analysis/bin/mcp-console", ["mcp-console", operation])


def frame(tag: int, value: object) -> None:
    payload = json.dumps(value).encode() + (b"\n" if tag == 2 else b"")
    sys.stdout.buffer.write(struct.pack(">BI", tag, len(payload)) + payload)
    sys.stdout.buffer.flush()


length = struct.unpack(">I", sys.stdin.buffer.read(4))[0]
bootstrap = json.loads(sys.stdin.buffer.read(length))
frame(1, {"version": bootstrap["version"], "build": bootstrap["build"]})
frame(2, {"kind": "ready"})
for line in sys.stdin.buffer:
    command = json.loads(line)
    if command["kind"] == "evaluate":
        for callback in (
            {"kind": "resolve_r", "packages": ["praise"]},
            {
                "kind": "resolve_python",
                "request": {
                    "requirements": {"packages": []},
                    "retained_requirements": {"packages": []},
                },
            },
            {"kind": "resolve_python_version", "request": {"constraints": [">=3.11"]}},
        ):
            frame(2, callback)
            response = json.loads(sys.stdin.buffer.readline())
            assert response["kind"].endswith("resolution_failed"), response
            assert (
                "dynamic environment resolution is unavailable" in response["message"]
            ), response
            frame(2, {"kind": "console_output", "data": response["message"] + "\n"})
        frame(2, {"kind": "completed"})
    elif command["kind"] == "shutdown":
        frame(2, {"kind": "shutdown_started"})
        frame(2, {"kind": "worker_exited", "code": 0})
        frame(3, {"confirmed": True, "error": None})
        break
