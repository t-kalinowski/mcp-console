"""Independent client for the versioned private SSH ownership protocol."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import struct
import subprocess

from support.ssh import read_frame

ZERO = {"id": 0, "hash": [0] * 32}


def encoded(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def frame(tag, body):
    return struct.pack(">BI", tag, len(body)) + body


def signature(key, *parts):
    return hmac.digest(bytes(key), b"".join(parts), "sha256")


def anchor(cursor, kind, data):
    identity = cursor["id"] + 1
    digest = hashlib.sha256(
        bytes(cursor["hash"]) + struct.pack(">Q", identity) + bytes([kind]) + data
    ).digest()
    return {"id": identity, "hash": list(digest)}


class Wire:
    def __init__(self, source, destination, key, epoch, remote=False):
        self.source, self.destination = source, destination
        self.key, self.epoch, self.remote = key, epoch, remote
        self.sent, self.received = 0, 0

    def encode(self, tag, body):
        self.sent += 1
        payload = struct.pack(">Q", self.sent) + body
        mac = signature(
            self.key, bytes([self.remote, tag]), struct.pack(">Q", self.epoch), payload
        )
        return frame(tag, payload + mac)

    def send(self, tag, body):
        payload = memoryview(self.encode(tag, body))
        while payload:
            payload = payload[self.destination.write(payload) :]
        self.destination.flush()

    def receive(self, timeout=15):
        tag, data = read_frame(self.source, timeout)
        assert len(data) >= 40
        payload, mac = data[:-32], data[-32:]
        identity = struct.unpack(">Q", payload[:8])[0]
        assert identity == self.received + 1
        assert hmac.compare_digest(
            mac,
            signature(
                self.key,
                bytes([not self.remote, tag]),
                struct.pack(">Q", self.epoch),
                payload,
            ),
        )
        self.received = identity
        return tag, payload[8:]


class Owner:
    def __init__(self, binary, *, lease_ms=1500, operation="ssh-prepare", generation=0):
        self.binary = binary
        self.secret = os.urandom(32)
        self.hello = {
            "version": 2,
            "build": subprocess.check_output([binary, "--version"], text=True).split()[
                1
            ],
            "operation": operation,
            "lease_ms": lease_ms,
            "owner": os.urandom(16).hex(),
            "generation": generation,
        }
        self.directory = Path("/tmp") / ("mcp-console-" + self.hello["owner"])
        self.tx = dict(ZERO)
        self.rx = dict(ZERO)
        self.processes = []
        self.epoch = 0

    def attach(self, *, secret=None, epoch=None, overrides=None):
        self.epoch = epoch if epoch is not None else self.epoch + 1
        key = self.secret if secret is None else secret
        request = {
            "hello": {**self.hello, **(overrides or {})},
            "epoch": self.epoch,
            "nonce": list(os.urandom(32)),
            "create": not self.processes,
        }
        binding = encoded(request)
        if request["create"]:
            request["secret"] = list(key)
        process = subprocess.Popen(
            [self.binary, "ssh-tunnel", self.hello["operation"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.processes.append(process)
        process.stdin.write(frame(1, encoded(request)))
        tag, body = read_frame(process.stdout)
        assert tag == 1, (tag, body)
        challenge = json.loads(body)
        nonce = bytes(challenge["nonce"])
        assert hmac.compare_digest(
            bytes(challenge["mac"]), signature(key, b"owner", binding, nonce)
        ), "owner authentication failed"
        process.stdin.write(frame(8, signature(key, b"controller", binding, nonce)))
        wire = Wire(
            process.stdout,
            process.stdin,
            signature(key, b"attachment", binding, nonce),
            self.epoch,
        )
        tag, body = wire.receive()
        assert tag == 9
        assert json.loads(body)["id"] <= self.tx["id"]
        wire.send(9, encoded(self.rx))
        return wire

    def receive(self, wire, *, respond=True):
        tag, body = wire.receive()
        if tag == 4 and respond:
            wire.send(5, body)
        elif tag == 2:
            identity = struct.unpack(">Q", body[:8])[0]
            previous, kind, data = body[8:40], body[40], body[41:]
            assert identity == self.rx["id"] + 1 and previous == bytes(self.rx["hash"])
            self.rx = anchor(self.rx, kind, data)
            wire.send(3, encoded(self.rx))
        return tag, body

    def data(self, wire, data, kind=0):
        body = (
            struct.pack(">Q", self.tx["id"] + 1)
            + bytes(self.tx["hash"])
            + bytes([kind])
            + data
        )
        self.tx = anchor(self.tx, kind, data)
        wire.send(2, body)
        return body

    def started(self, wire):
        # Two challenges prove that the first round trip started the helper.
        for _ in range(2):
            while self.receive(wire)[0] != 4:
                pass

    def close(self):
        for process in self.processes:
            if not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()
