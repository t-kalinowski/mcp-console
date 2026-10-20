"""Measure server allocation requests through the public MCP process."""

import mmap
import struct
from pathlib import Path

from support.native import LOADER_VARIABLE, build_interposer


class AllocationProfile:
    def __init__(self, root: Path) -> None:
        path = root / "allocations"
        path.write_bytes(bytes(32))
        with path.open("r+b") as stream:
            self.mapping = mmap.mmap(stream.fileno(), 32)
        self.environment = {
            LOADER_VARIABLE: str(build_interposer(root, "preview_allocations")),
            "MCP_CONSOLE_TEST_ALLOCATION_PROFILE": str(path),
        }

    def start(self) -> None:
        struct.pack_into("=QQ", self.mapping, 8, 0, 0)
        struct.pack_into("=Q", self.mapping, 0, 1)

    def stop(self) -> tuple[int, int]:
        struct.pack_into("=Q", self.mapping, 0, 0)
        return struct.unpack_from("=QQ", self.mapping, 8)

    def pause_results(self, pause: bool) -> None:
        struct.pack_into("=Q", self.mapping, 24, int(pause))

    def close(self) -> None:
        self.mapping.close()


class RetentionProfile:
    """Live large allocations and peak growth, independent of allocator caches."""

    def __init__(self, root: Path) -> None:
        path = root / "retained-allocations"
        path.write_bytes(bytes(32))
        with path.open("r+b") as stream:
            self.mapping = mmap.mmap(stream.fileno(), 32)
        self.environment = {
            LOADER_VARIABLE: str(build_interposer(root, "recording_retention")),
            "MCP_CONSOLE_TEST_RETENTION_PROFILE": str(path),
        }

    @property
    def live(self) -> int:
        return struct.unpack_from("=q", self.mapping)[0]

    def start(self) -> None:
        struct.pack_into("=qqQ", self.mapping, 8, self.live, 0, 1)

    def stop(self) -> int:
        struct.pack_into("=Q", self.mapping, 24, 0)
        return struct.unpack_from("=q", self.mapping, 16)[0]

    def close(self) -> None:
        self.mapping.close()
