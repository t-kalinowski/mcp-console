"""Deterministic local failure peer for the SSH transport adapter boundary."""

import json
import os
import struct
import sys
from pathlib import Path


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

    preparation_frame({"Hello": {"version": 6, "build": bootstrap["Open"]["build"]}})
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
frame(
    1,
    {
        "version": 999
        if mode == "incompatible"
        else 9
        if mode == "prior-bootstrap-protocol"
        else bootstrap["version"],
        "build": bootstrap["build"],
    },
)
if mode == "incompatible":
    sys.exit(0)
frame(2, {"kind": "ready"})
if (
    mode == "bootstrap-input-completion"
    and not (log.parent / "invalid-bootstrap-sent").exists()
):
    frame(2, {"kind": "input_requested", "prompt": "startup> "})
    with (log.parent / "finish-bootstrap").open("rb", buffering=0) as gate:
        assert gate.read(1) == b"1"
    (log.parent / "invalid-bootstrap-sent").touch()
    frame(
        2,
        {
            "kind": "runtime_initialized",
            "interrupted": os.environ["CONSOLE_BOOTSTRAP_COMPLETE"] == "0",
        },
    )
elif mode not in {"bootstrap-interrupted", "prior-bootstrap-protocol"}:
    frame(2, {"kind": "runtime_initialized", "interrupted": False})
for line in sys.stdin.buffer:
    command = json.loads(line)
    with log.open("a") as output:
        output.write(json.dumps(command) + "\n")
    if command["kind"] == "evaluate":
        if mode in {"bootstrap-interrupted", "bootstrap-input-completion"}:
            if command["source"] == "never_run = True":
                (log.parent / "cell-ran").touch()
            frame(2, {"kind": "console_output", "data": "42\n"})
        if mode == "lost":
            sys.exit(255)
        if mode == "resolver":
            callbacks = {
                "resolve_r": {"kind": "resolve_r", "packages": ["praise"]},
                "resolve_python": {
                    "kind": "resolve_python",
                    "request": {
                        "requirements": {"packages": []},
                        "retained_requirements": {"packages": []},
                    },
                },
                "resolve_python_version": {
                    "kind": "resolve_python_version",
                    "request": {"constraints": [">=3.11"]},
                },
            }
            frame(2, callbacks[os.environ["CONSOLE_SSH_CALLBACK"]])
            response = json.loads(sys.stdin.buffer.readline())
            assert response["kind"].endswith("resolution_failed"), response
            assert (
                "dynamic environment resolution is unavailable" in response["message"]
            ), response
            frame(2, {"kind": "console_output", "data": response["message"] + "\n"})
        frame(2, {"kind": "completed"})
    elif command["kind"] == "interrupt" and mode == "bootstrap-interrupted":
        frame(2, {"kind": "interrupt_result", "request_id": command["request_id"]})
        with (log.parent / "interrupt-bootstrap").open("rb", buffering=0) as gate:
            assert gate.read(1) == b"1"
        frame(2, {"kind": "runtime_initialized", "interrupted": True})
        frame(2, {"kind": "console_output", "data": "bootstrap interrupted\n"})
    elif command["kind"] == "shutdown":
        frame(2, {"kind": "shutdown_started"})
        frame(2, {"kind": "worker_exited", "code": 0})
        frame(3, {"confirmed": True, "error": None})
        sys.exit(0)
