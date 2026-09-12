"""Authenticated envelope for deliberately invalid inner protocol peers."""

import atexit
import io
import json
import os
from pathlib import Path
import select
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support.ssh import read_frame
from support.ssh_protocol import ZERO, Wire, anchor, encoded, frame, signature


def wrap():
    source = os.fdopen(os.dup(0), "rb", buffering=0)
    destination = sys.stdout.buffer
    tag, body = read_frame(source)
    assert tag == 1
    request = json.loads(body)
    if not request["create"]:
        destination.write(
            frame(10, b"fake SSH owner is missing; retirement is unconfirmed")
        )
        destination.flush()
        raise SystemExit(1)
    secret = bytes(request.pop("secret"))
    assert request["hello"]["version"] == 2
    binding = encoded(request)
    nonce = os.urandom(32)
    destination.write(
        frame(
            1,
            encoded(
                {
                    "nonce": list(nonce),
                    "mac": list(signature(secret, b"owner", binding, nonce)),
                }
            ),
        )
    )
    destination.flush()
    assert read_frame(source) == (8, signature(secret, b"controller", binding, nonce))
    wire = Wire(
        source,
        destination,
        signature(secret, b"attachment", binding, nonce),
        request["epoch"],
        remote=True,
    )
    wire.send(9, encoded(ZERO))
    assert wire.receive() == (9, encoded(ZERO))
    ping = os.urandom(32)
    wire.send(4, ping)
    interval = request["hello"]["lease_ms"] / 6000

    class Stream(io.RawIOBase):
        def __init__(self):
            self.pending = bytearray()
            self.sent = dict(ZERO)
            self.received = dict(ZERO)
            self.eof, self.ended = False, False
            self.ping = ping
            self.next_ping = time.monotonic() + interval

        def readable(self):
            return True

        def writable(self):
            return True

        def receive(self):
            if (
                self.ping is None
                and not select.select(
                    [source], [], [], max(0, self.next_ping - time.monotonic())
                )[0]
            ):
                self.ping = os.urandom(32)
                wire.send(4, self.ping)
            tag, body = wire.receive()
            if tag == 2:
                assert int.from_bytes(body[:8], "big") == self.received["id"] + 1
                assert body[8:40] == bytes(self.received["hash"]) and body[40] == 0
                self.pending.extend(body[41:])
                self.received = anchor(self.received, 0, body[41:])
                wire.send(3, encoded(self.received))
            elif tag == 5:
                assert body == self.ping
                self.ping = None
                self.next_ping = time.monotonic() + interval
            elif tag == 6:
                assert json.loads(body) == self.received
                self.eof = True
                wire.send(7, encoded(self.received))
            elif tag == 7:
                self.ended = True
            else:
                assert tag == 3, tag

        def readinto(self, buffer):
            while not self.pending and not self.eof:
                self.receive()
            count = min(len(buffer), len(self.pending))
            buffer[:count] = self.pending[:count]
            del self.pending[:count]
            return count

        def write(self, data):
            for offset in range(0, len(data), 16384):
                chunk = bytes(data[offset : offset + 16384])
                body = (
                    (self.sent["id"] + 1).to_bytes(8, "big")
                    + bytes(self.sent["hash"])
                    + b"\0"
                    + chunk
                )
                self.sent = anchor(self.sent, 0, chunk)
                wire.send(2, body)
            return len(data)

        def finish(self):
            try:
                sys.stdout.flush()
                wire.send(6, encoded(self.sent))
                while not self.ended:
                    self.receive()
            except (OSError, AssertionError):
                pass

    stream = Stream()
    sys.stdin = io.TextIOWrapper(io.BufferedReader(stream))
    sys.stdout = io.TextIOWrapper(io.BufferedWriter(stream), write_through=True)
    atexit.register(stream.finish)
