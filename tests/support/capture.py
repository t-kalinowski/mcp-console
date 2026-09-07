import json
import os
import selectors
import time
from pathlib import Path
from typing import TextIO

from support.records import Transcript


def read_jsonl(stream: TextIO) -> Transcript:
    return [json.loads(line) for line in stream.read().splitlines()]


def read_jsonl_path(path: Path) -> Transcript:
    with path.open(encoding="utf-8") as stream:
        return read_jsonl(stream)


def read_lines(
    stream: object,
    count: int,
    description: str,
    *,
    timeout: float = 10,
) -> list[str]:
    descriptor = stream.fileno()  # type: ignore[attr-defined]
    output = bytearray()
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        while output.count(b"\n") < count:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"timed out waiting for {description}"
            ready = selector.select(remaining)
            assert ready, f"timed out waiting for {description}"
            chunk = os.read(descriptor, 4096)
            assert chunk, f"sandbox closed before reporting {description}"
            output.extend(chunk)
    lines = output.decode("utf-8").splitlines()
    assert len(lines) == count, (description, lines)
    return lines
