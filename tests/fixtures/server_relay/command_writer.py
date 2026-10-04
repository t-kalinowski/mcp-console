#!/usr/bin/env python3
"""Real pipe holders for server command-writer retirement contracts."""

import json
import os
import signal as signals
import sys
from pathlib import Path

root = Path(os.environ["MCP_CONSOLE_TEST_WRITER_ROOT"])
mode = os.environ["MCP_CONSOLE_TEST_WRITER_MODE"]
generation_path = root / "generation"
generation = int(generation_path.read_text()) + 1 if generation_path.exists() else 1
generation_path.write_text(str(generation))


def signal(name: str) -> None:
    with (root / name).open("wb", buffering=0) as checkpoint:
        assert checkpoint.write(b"1") == 1


def wait(name: str) -> None:
    with (root / name).open("rb", buffering=0) as checkpoint:
        assert checkpoint.read(1) == b"1"


def send(kind: str, **values: object) -> None:
    sys.stdout.write(json.dumps({"kind": kind, **values}) + "\n")
    sys.stdout.flush()


def receive() -> dict:
    return json.loads(sys.stdin.buffer.readline())


send("ready")
assert receive() == {"kind": "evaluate", "language": "r", "source": "writer-probe"}
send("completed")

if generation <= int(os.environ["MCP_CONSOLE_TEST_WRITER_RESTARTS"]):
    if mode.startswith("interrupt"):
        command = receive()
        assert command["kind"] == "interrupt", command
        signal(f"interrupt-{generation}")
        # Withhold the acknowledgment until ordered shutdown reaches the relay.
        shutdown = receive()
        assert shutdown["kind"] == "shutdown", shutdown
        send("shutdown_started")
        signal("shutdown-1")
        if mode == "interrupt_after_eof":
            assert sys.stdin.buffer.read() == b""
        else:
            wait("ack-1")
        send("interrupt_result", request_id=command["request_id"], error=None)
        if mode == "interrupt_after_eof":
            (root / "late-ack").touch()
    else:
        if mode != "stdout":
            # Consume only the beginning of the frame, then stop reading. The
            # multi-megabyte payload cannot fit in this real command pipe.
            assert os.read(0, 1) == b"{"
            signal(f"partial-{generation}")
        admitted, notify = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(admitted)
            if mode == "stdout":
                os.close(0)
            else:
                os.close(1)
            os.close(2)
            os.write(notify, b"1")
            os.close(notify)
            wait(f"holder-{generation}")
            os._exit(0)
        os.close(notify)
        (root / f"holder-pid-{generation}").write_text(str(pid))
        assert os.read(admitted, 1) == b"1"
        os.close(admitted)
        signal(f"held-{generation}")
        if mode == "forced":
            # The launcher remains live until its server escalates retirement.
            while True:
                signals.pause()
        wait(f"exit-{generation}")
        os._exit(0)
else:
    assert receive()["kind"] == "shutdown"
    send("shutdown_started")

send("stdout_closed")
send("stderr_closed")
send("worker_sideband_closed")
send("worker_exited", code=0)
