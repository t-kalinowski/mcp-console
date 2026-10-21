#!/usr/bin/env python3
"""Controlled worker receipts and native diagnostics at capture boundaries."""

import os
from pathlib import Path

from scripted_relay import ScriptedRelay, write_all


def gate(name: str) -> None:
    with (Path(os.environ["CAPTURE_GATES"]) / name).open("rb", buffering=0) as stream:
        assert stream.read(1) == b"1"


def notify(name: str) -> None:
    with (Path(os.environ["CAPTURE_GATES"]) / name).open("wb", buffering=0) as stream:
        stream.write(b"1")


mode = os.environ["CAPTURE_SCENARIO"]
relay = ScriptedRelay(acquired=mode != "process_output")
try:
    if mode == "process_output":
        write_all(2, b"process head\n" + b"p" * 32768 + b"\nprocess tail\n")
        gate("release")
        relay.send({"kind": "worker_started", "pid": 42})
    relay.ready()
    command = relay.receive(cell_started=mode != "late_entry")
    if command["kind"] == "shutdown":
        relay.retire(command)
    elif mode == "invalid_completion":
        relay.send({"kind": "input_requested", "prompt": "pending> "})
        relay.send({"kind": "completed"})
        relay.retire()
    elif mode == "late_entry":
        notify("evaluating")
        command = relay.receive()
        assert command["kind"] == "shutdown", command
        relay.send({"kind": "shutdown_started"})
        notify("shutdown")
        gate("release")
        relay.send({"kind": "cell_started"})
        relay.send({"kind": "console_output", "data": "entered during retirement\n"})
        relay.send({"kind": "completed"})
        for kind in ("stdout_closed", "stderr_closed", "worker_sideband_closed"):
            relay.send({"kind": kind})
        relay.send({"kind": "worker_exited", "code": 0})
    elif mode == "ended_owner":
        relay.send({"kind": "completed"})
        relay.send({"kind": "console_output", "data": "post-cell text\n"})
        notify("post_sent")
        relay.retire()
        gate("release")
        write_all(2, b"after worker exit\n")
        gate("drained")
    else:
        assert mode == "process_output", mode
        relay.send({"kind": "completed"})
        relay.retire()
finally:
    relay.close()
