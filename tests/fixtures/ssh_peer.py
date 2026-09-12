"""Deterministic local failure peer for the SSH transport adapter boundary."""

import json
import os
import struct
import sys
from pathlib import Path

from ssh_tunnel_peer import wrap

wrap()


def frame(tag: int, value: object) -> None:
    body = json.dumps(value).encode()
    if tag == 2:
        body += b"\n"
    sys.stdout.buffer.write(struct.pack(">BI", tag, len(body)) + body)
    sys.stdout.buffer.flush()


mode = os.environ["CONSOLE_SSH_PEER"]
log = Path(os.environ["CONSOLE_SSH_PEER_LOG"])
length = struct.unpack(">I", sys.stdin.buffer.read(4))[0]
bootstrap = json.loads(sys.stdin.buffer.read(length))
if "Open" in bootstrap:

    def preparation_frame(value):
        body = json.dumps(value).encode()
        sys.stdout.buffer.write(struct.pack(">I", len(body)) + body)
        sys.stdout.buffer.flush()

    preparation_frame({"Hello": {"version": 3, "build": bootstrap["Open"]["build"]}})
    preparation_frame(
        {
            "Completed": {
                "id": 0,
                "result": {
                    "Ok": {
                        "managed": False,
                        "selections": {"r_home": None, "python": None},
                    }
                },
                "control": None,
                "confirmed": True,
            }
        }
    )
    length = struct.unpack(">I", sys.stdin.buffer.read(4))[0]
    assert json.loads(sys.stdin.buffer.read(length)) == "Close"
    preparation_frame("Closed")
    sys.exit(0)
with log.open("a") as output:
    output.write("launched\n")
assert "-T" in sys.argv and "-a" in sys.argv and "BatchMode=yes" in sys.argv
assert sys.argv[-2] == "console-test", sys.argv
if mode == "auth":
    print("Permission denied (publickey).", file=sys.stderr)
    sys.exit(255)
if mode == "stdout":
    print("unexpected login banner", flush=True)
    sys.exit(0)
frame(1, {"version": 999 if mode == "incompatible" else 2, "build": bootstrap["build"]})
if mode == "incompatible":
    sys.exit(0)
frame(2, {"kind": "ready"})
for line in sys.stdin.buffer:
    command = json.loads(line)
    with log.open("a") as output:
        output.write(json.dumps(command) + "\n")
    if command["kind"] == "evaluate":
        if mode == "lost":
            sys.exit(255)
        if mode == "resolver":
            callbacks = [
                {"kind": "resolve_r", "packages": ["praise"]},
                {
                    "kind": "resolve_python",
                    "request": {
                        "requirements": {"packages": []},
                        "retained_requirements": {"packages": []},
                    },
                },
                {
                    "kind": "resolve_python_version",
                    "request": {"constraints": [">=3.11"]},
                },
            ]
            for callback in callbacks:
                frame(2, callback)
                response = json.loads(sys.stdin.buffer.readline())
                assert response["kind"].endswith("resolution_failed"), response
                assert (
                    "dynamic environment resolution is unavailable"
                    in response["message"]
                ), response
                frame(2, {"kind": "console_output", "data": response["message"] + "\n"})
        frame(2, {"kind": "completed"})
    elif command["kind"] == "shutdown":
        frame(2, {"kind": "shutdown_started"})
        frame(2, {"kind": "worker_exited", "code": 0})
        frame(3, {"confirmed": True, "error": None})
        sys.exit(0)
