"""Deterministic local failure peer for the SSH transport adapter boundary."""

import base64
import json
import os
import struct
import sys
import threading
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
if mode == "blocked-command-bootstrap" and length > 512 * 1024:
    # This is the target launch, after the ordinary preparation handshake.
    # Keep only stdin open without consuming the oversized bootstrap body.
    with (log.parent / "bootstrap-partial").open("wb", buffering=0) as signal:
        assert signal.write(b"1") == 1
    admitted, notify = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(admitted)
        os.close(1)
        os.close(2)
        os.write(notify, b"1")
        os.close(notify)
        with (log.parent / "bootstrap-holder").open("rb", buffering=0) as gate:
            assert gate.read(1) == b"1"
        os._exit(0)
    os.close(notify)
    (log.parent / "bootstrap-holder-pid").write_text(str(pid))
    assert os.read(admitted, 1) == b"1"
    os.close(admitted)
    with (log.parent / "bootstrap-held").open("wb", buffering=0) as signal:
        assert signal.write(b"1") == 1
    with (log.parent / "bootstrap-exit").open("rb", buffering=0) as gate:
        assert gate.read(1) == b"1"
    os._exit(0)
bootstrap = json.loads(sys.stdin.buffer.read(length))
if "Open" in bootstrap:

    def preparation_frame(value):
        body = json.dumps(value).encode()
        sys.stdout.buffer.write(struct.pack(">I", len(body)) + body)
        sys.stdout.buffer.flush()

    preparation_frame({"Hello": {"version": 6, "build": bootstrap["Open"]["build"]}})
    if mode in {"discovery-diagnostics", "discovery-image"}:
        print("preparation detail", file=sys.stderr, flush=True)
        with (log.parent / "discovery-started").open("wb", buffering=0) as signal:
            assert signal.write(b"1") == 1
        with (log.parent / "discovery-release").open("rb", buffering=0) as gate:
            assert gate.read(1) == b"1"
    if mode == "diagnostic-overlap":
        sys.stderr.buffer.write(b"producer prefix \xce")
        sys.stderr.buffer.flush()
        with (log.parent / "discovery-release").open("rb", buffering=0) as gate:
            assert gate.read(1) == b"1"

        def finish_diagnostic():
            with (log.parent / "diagnostic-release").open("rb", buffering=0) as gate:
                assert gate.read(1) == b"1"
            sys.stderr.buffer.write(b"\xb1 diagnostic complete\n")
            sys.stderr.buffer.flush()

        diagnostic = threading.Thread(target=finish_diagnostic)
        diagnostic.start()
    if mode == "diagnostic-terminal-overlap":

        def progress_diagnostic():
            for checkpoint, data in (
                ("diagnostic-start", b"progress 1\r"),
                ("diagnostic-finish", b"progress 2\n"),
            ):
                with (log.parent / checkpoint).open("rb", buffering=0) as gate:
                    assert gate.read(1) == b"1"
                sys.stderr.buffer.write(data)
                sys.stderr.buffer.flush()

        diagnostic = threading.Thread(target=progress_diagnostic)
        diagnostic.start()
    if mode in {"discovery-failure", "discovery-diagnostics-failure"}:
        if mode == "discovery-diagnostics-failure":
            print("preparation failure detail", file=sys.stderr, flush=True)
        with (log.parent / "discovery-started").open("wb", buffering=0) as signal:
            assert signal.write(b"1") == 1
        with (log.parent / "discovery-release").open("rb", buffering=0) as gate:
            assert gate.read(1) == b"1"
        preparation_frame(
            {
                "Completed": {
                    "id": 0,
                    "result": {"Err": "synthetic discovery failure"},
                    "control": None,
                    "confirmed": True,
                }
            }
        )
        length = struct.unpack(">I", sys.stdin.buffer.read(4))[0]
        assert json.loads(sys.stdin.buffer.read(length)) == "Close"
        preparation_frame("Closed")
        sys.exit(0)
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
    if mode in {"diagnostic-overlap", "diagnostic-terminal-overlap"}:
        diagnostic.join()
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
if mode in {"incompatible", "prior-bootstrap-protocol"}:
    # Rejected handshakes cannot publish Ready on the closed event transport.
    sys.exit(0)
frame(2, {"kind": "ready"})
if mode == "startup-recording-failure":
    stream = os.environ["CONSOLE_TEST_DIRECT_STREAM"]
    frame(
        2,
        {"kind": stream + "_bytes", "data": base64.b64encode(b"\xce").decode()},
    )
    with (log.parent / "partial-release").open("rb", buffering=0) as gate:
        assert gate.read(1) == b"1"
    frame(
        2,
        {"kind": stream + "_bytes", "data": base64.b64encode(b"\xb1\n").decode()},
    )
if mode == "discovery-image":
    frame(
        2,
        {
            "kind": "image",
            "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
            "mime_type": "image/png",
        },
    )
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
        if mode == "direct-diagnostic-overlap":
            stream = os.environ["CONSOLE_TEST_DIRECT_STREAM"]
            frame(
                2,
                {"kind": stream + "_bytes", "data": base64.b64encode(b"\xce").decode()},
            )
            with (log.parent / "evaluation-started").open("wb", buffering=0) as signal:
                assert signal.write(b"1") == 1
            with (log.parent / "diagnostic-start").open("rb", buffering=0) as gate:
                assert gate.read(1) == b"1"
            print("launcher detail", file=sys.stderr, flush=True)
            with (log.parent / "evaluation-finish").open("rb", buffering=0) as gate:
                assert gate.read(1) == b"1"
            frame(
                2,
                {
                    "kind": stream + "_bytes",
                    "data": base64.b64encode(b"\xb1\n").decode(),
                },
            )
            frame(2, {"kind": "completed"})
            continue
        if mode == "diagnostic-terminal-overlap":
            sys.stderr.buffer.write(b"diagnostic reader ready\n")
            sys.stderr.buffer.flush()
            with (log.parent / "diagnostic-close").open("rb", buffering=0) as gate:
                assert gate.read(1) == b"1"
            os.close(2)
            with (log.parent / "evaluation-finish").open("rb", buffering=0) as gate:
                assert gate.read(1) == b"1"
            frame(2, {"kind": "completed"})
            continue
        if mode == "diagnostic-overlap":
            with (log.parent / "evaluation-started").open("wb", buffering=0) as signal:
                assert signal.write(b"1") == 1
            continue
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
