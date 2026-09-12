"""Lease envelope for deliberately invalid inner protocol peers."""

import atexit
import io
import json
import os
import select
import struct
import sys
import time


def wrap():
    source = os.fdopen(os.dup(0), "rb", buffering=0)
    destination = sys.stdout.buffer

    def exact(length):
        result = bytearray()
        while len(result) < length:
            chunk = source.read(length - len(result))
            assert chunk, "truncated lease frame"
            result.extend(chunk)
        return bytes(result)

    def read_frame():
        header = exact(5)
        assert len(header) == 5, header
        tag, length = struct.unpack(">BI", header)
        assert length <= 32768
        body = exact(length)
        assert len(body) == length
        return tag, body

    def write_frame(tag, body):
        destination.write(struct.pack(">BI", tag, len(body)) + body)
        destination.flush()

    tag, body = read_frame()
    assert tag == 1
    hello = json.loads(body)
    assert hello["version"] == 1
    write_frame(1, body)
    token = os.urandom(32)
    write_frame(4, token)
    assert read_frame() == (5, token)
    interval = hello["lease_ms"] / 6000

    class Stream(io.RawIOBase):
        def __init__(self):
            self.pending = bytearray()
            self.sent = 0
            self.received = 0
            self.eof = False
            self.ended = False
            self.ping = None
            self.next_ping = time.monotonic() + interval

        def readable(self):
            return True

        def writable(self):
            return True

        def receive(self):
            if self.ping is None:
                if not select.select(
                    [source], [], [], max(0, self.next_ping - time.monotonic())
                )[0]:
                    self.ping = os.urandom(32)
                    write_frame(4, self.ping)
            tag, body = read_frame()
            if tag == 2:
                offset = struct.unpack(">Q", body[:8])[0]
                assert offset == self.received
                self.pending.extend(body[8:])
                self.received += len(body) - 8
                write_frame(3, struct.pack(">Q", self.received))
            elif tag == 5:
                assert body == self.ping
                self.ping = None
                self.next_ping = time.monotonic() + interval
            elif tag == 6:
                assert struct.unpack(">Q", body)[0] == self.received
                self.eof = True
                write_frame(7, b"")
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
                chunk = data[offset : offset + 16384]
                write_frame(2, struct.pack(">Q", self.sent) + chunk)
                self.sent += len(chunk)
            return len(data)

        def finish(self):
            try:
                sys.stdout.flush()
                write_frame(6, struct.pack(">Q", self.sent))
                while not self.ended:
                    self.receive()
            except (OSError, AssertionError):
                pass

    stream = Stream()
    sys.stdin = io.TextIOWrapper(io.BufferedReader(stream))
    sys.stdout = io.TextIOWrapper(io.BufferedWriter(stream), write_through=True)
    atexit.register(stream.finish)
