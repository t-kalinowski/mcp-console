"""Ordered target sideband peer with the real host preparation protocol."""

import json
import os
import struct
import sys
from pathlib import Path

binary = os.environ["CONSOLE_BOOTSTRAP_ORDER_BINARY"]
if "ssh-prepare" in sys.argv[-1]:
    os.execv(binary, [binary, "ssh-prepare"])

root = Path(os.environ["CONSOLE_BOOTSTRAP_ORDER_ROOT"])
length = struct.unpack(">I", sys.stdin.buffer.read(4))[0]
bootstrap = json.loads(sys.stdin.buffer.read(length))


def frame(tag: int, value: dict) -> None:
    payload = json.dumps(value).encode() + (b"\n" if tag == 2 else b"")
    sys.stdout.buffer.write(struct.pack(">BI", tag, len(payload)) + payload)
    sys.stdout.buffer.flush()


frame(1, {"version": bootstrap["version"], "build": bootstrap["build"]})
frame(2, {"kind": "ready"})
requirements = dict(bootstrap["environment"]["python"]["requirements"])
requirements["packages"] = [*requirements["packages"], "py-yaml12"]
frame(
    2,
    {
        "kind": "resolve_python",
        "request": {
            "requirements": requirements,
            "retained_requirements": requirements,
            "initialized": True,
            "import_resolution": {"module": "yaml12", "distribution": "py-yaml12"},
        },
    },
)
response = json.loads(sys.stdin.buffer.readline())
assert response["kind"] == "python_resolved", response
with (root / "ready").open("wb", buffering=0) as gate:
    assert gate.write(b"1") == 1
with (root / "events").open("rb", buffering=0) as gate:
    assert gate.read(1) == b"1"
frame(2, {"kind": "python_activated", "requirements": requirements})
frame(2, {"kind": "runtime_initialized", "complete": True})
frame(2, {"kind": "console_output", "data": "after activation\n"})
frame(
    2,
    {
        "kind": "image",
        "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
        "mime_type": "image/png",
    },
)
frame(2, {"kind": "input_requested", "prompt": "startup> "})
for index in range(int(os.environ.get("CONSOLE_BOOTSTRAP_ORDER_NOISE", "0"))):
    frame(
        2,
        {"kind": "console_output", "data": f"queued {index:06d} " + "x" * 1024 + "\n"},
    )
# Direct stdout is an independent producer and may pass deferred sideband
# semantics; its position is not part of the activation-order assertion.
frame(2, {"kind": "stdout", "data": "ordered receipt\n"})
with (root / "published").open("wb", buffering=0) as gate:
    assert gate.write(b"1") == 1
with (root / "resuming").open("rb", buffering=0) as gate:
    assert gate.read(1) == b"1"
frame(2, {"kind": "console_output", "data": "after resumption\n"})
for line in sys.stdin.buffer:
    command = json.loads(line)
    if command["kind"] == "stdin":
        assert command["data"] == "continue\n", command
        frame(2, {"kind": "input_received"})
    elif command["kind"] == "evaluate":
        frame(2, {"kind": "console_output", "data": "42\n"})
        frame(2, {"kind": "completed"})
    elif command["kind"] == "shutdown":
        frame(2, {"kind": "shutdown_started"})
        frame(2, {"kind": "stdout_closed"})
        frame(2, {"kind": "stderr_closed"})
        frame(2, {"kind": "worker_sideband_closed"})
        frame(2, {"kind": "worker_exited", "code": 0})
        frame(3, {"confirmed": True, "error": None})
        break
    else:
        raise AssertionError(command)
