import os
import select
import time
from pathlib import Path
from typing import Self

from support.client import McpClient


class FifoCheckpoint:
    def __init__(self, path: Path, *, create: bool) -> None:
        self.path = path
        if create:
            os.mkfifo(path)
        # Keep a writer open so an early release cannot strand a later reader
        # in its blocking open.
        self.descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    @classmethod
    def create(cls, path: Path) -> Self:
        return cls(path, create=True)

    @classmethod
    def attach(cls, path: Path) -> Self:
        return cls(path, create=False)

    def close(self) -> None:
        os.close(self.descriptor)

    def wait(self, description: str | None = None, timeout: float = 10) -> None:
        readable, _, _ = select.select([self.descriptor], [], [], timeout)
        assert readable, f"checkpoint was not reached: {description or self.path.name}"
        assert os.read(self.descriptor, 1) == b"1"

    def release(self) -> None:
        assert os.write(self.descriptor, b"1") == 1


def wait_for_worker_file(root: Path, name: str, client: McpClient) -> Path:
    deadline = time.monotonic() + 10
    while True:
        paths = list(root.glob(f"**/{name}"))
        if paths:
            assert len(paths) == 1, paths
            return paths[0]
        assert client.process.poll() is None, (
            "mcp-console stopped before worker checkpoint"
        )
        assert time.monotonic() < deadline, f"worker did not create {name}"
        time.sleep(0.01)


def release_fixture_checkpoint(path: Path) -> None:
    with path.open("wb", buffering=0) as stream:
        assert stream.write(b"1") == 1


def release_partial_sideband(marker: Path) -> None:
    release = marker.with_name("zod-release-partial-sideband")
    with release.open("wb", buffering=0) as stream:
        assert stream.write(b"x") == 1
