"""One-shot frame loss around real OpenSSH owner acceptance and reply delivery.

Only public owner IDs are recorded. Capabilities and frame payloads stay in memory.
"""

import json
import os
from pathlib import Path
import select
import struct
import subprocess
import sys

root = Path(sys.argv[1])
role = sys.argv[-1]
process = subprocess.Popen(sys.argv[2:], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
(root / (role + ".attachment_pid")).write_text(str(process.pid))
sources = [sys.stdin.buffer, process.stdout]
destinations = [process.stdin, sys.stdout.buffer]
pending = [bytearray(), bytearray()]
accepted = None
terminal = None
ping_after_end = None


def output(index, data):
    destinations[index].write(data)
    destinations[index].flush()


def checkpoint(action):
    if action.get("checkpoint"):
        with open(action["checkpoint"], "wb", buffering=0) as checkpoint:
            checkpoint.write(b"1")


def cut(action):
    checkpoint(action)
    raise SystemExit(0)


try:
    while True:
        ready, _, _ = select.select(sources, [], [], 60)
        assert ready, "frame gate timed out"
        for index, source in enumerate(sources):
            if source not in ready:
                continue
            data = os.read(source.fileno(), 16384)
            if not data:
                raise SystemExit(0)
            pending[index].extend(data)
            while len(pending[index]) >= 5:
                tag, length = struct.unpack(">BI", pending[index][:5])
                assert length <= 32768
                if len(pending[index]) < 5 + length:
                    break
                message = bytes(pending[index][: 5 + length])
                del pending[index][: 5 + length]
                if index == 0 and tag == 1:
                    request = json.loads(message[5:])
                    (root / (role + ".owner")).write_text(request["hello"]["owner"])
                if accepted and index == 1 and tag == 3:
                    cursor = json.loads(message[13:-32])
                    if cursor["id"] >= accepted[0]:
                        cut(accepted[1])
                if terminal and index == 1 and tag == 4:
                    ping_after_end = message[13:-32]
                if (
                    terminal
                    and ping_after_end
                    and index == 0
                    and tag == 5
                    and message[13:-32] == ping_after_end
                ):
                    cut(terminal)
                action_path = root / (role + ".action")
                action = (
                    json.loads(action_path.read_text())
                    if action_path.exists()
                    else None
                )
                body = message[54:-32] if tag == 2 else message[5:]
                if (
                    action
                    and index == (action["direction"] == "down")
                    and tag == action.get("tag", 2)
                    and action.get("marker", "").encode() in body
                ):
                    action_path.unlink()
                    stage = action["stage"]
                    if stage == "hold":
                        checkpoint(action)
                        with open(action["release"], "rb", buffering=0) as release:
                            assert release.read(1) == b"1"
                    elif stage == "count":
                        action["remaining"] -= 1
                        if action["remaining"]:
                            action_path.write_text(json.dumps(action))
                        else:
                            checkpoint(action)
                    elif stage == "before":
                        cut(action)
                    elif stage == "partial":
                        output(index, message[:17])
                        cut(action)
                    elif stage == "after_end":
                        assert index == 1 and tag == 6
                        terminal = action
                    else:
                        assert stage == "accepted" and index == 0
                        accepted = (int.from_bytes(message[13:21], "big"), action)
                output(index, message)
finally:
    process.stdin.close()
    if process.poll() is None:
        process.terminate()
    process.wait(timeout=10)
