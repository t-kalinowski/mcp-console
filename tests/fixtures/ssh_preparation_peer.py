"""Deliberately invalid preparation peers, reached through real OpenSSH."""

import json
import struct
import sys
from pathlib import Path

mode, record, operation = sys.argv[1:]
assert operation == "ssh-prepare", operation


def read():
    header = sys.stdin.buffer.read(4)
    if not header:
        return None
    length = struct.unpack(">I", header)[0]
    return json.loads(sys.stdin.buffer.read(length))


def write(message):
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(struct.pack(">I", len(body)) + body)
    sys.stdout.buffer.flush()


def complete(id, value, confirmed=True):
    write(
        {
            "Completed": {
                "id": id,
                "result": {"Ok": value},
                "control": None,
                "confirmed": confirmed,
            }
        }
    )


opened = read()["Open"]
write(
    {
        "Hello": {
            "version": 2 if mode == "incompatible" else 3,
            "build": opened["build"],
        }
    }
)
if mode == "incompatible":
    raise SystemExit(0)
complete(
    0, {"managed": True, "selections": {"r_home": "/remote-only/R", "python": None}}
)
while (message := read()) is not None:
    if message == "Close":
        write("Closed")
        break
    request = message["Run"]
    with Path(record).open("a") as stream:
        stream.write(json.dumps(request) + "\n")
    id = request["id"]
    if mode in ("truncated-result", "mismatched-chunk", "chunked-and-inline"):
        write(
            {
                "ResultChunk": {
                    "id": id + (mode == "mismatched-chunk"),
                    "text": json.dumps({"Err": "installer failed"}),
                }
            }
        )
        if mode == "chunked-and-inline":
            complete(id, None)
        break
    if mode == "unconfirmed":
        complete(id, None, confirmed=False)
        break
    if mode == "truncated":
        sys.stdout.buffer.write(struct.pack(">I", 100) + b"{}")
        sys.stdout.buffer.flush()
        break
    if mode == "missing":
        break
    if mode == "mismatched":
        complete(id + 1, None)
        break
    if request["operation"] == "Bootstrap":
        complete(id, None)
    else:
        assert mode == "malformed", mode
        complete(id, 42)
