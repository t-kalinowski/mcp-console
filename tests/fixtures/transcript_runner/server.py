import os
from pathlib import Path


def test_initializes_and_lists_tools(binary: Path) -> list[dict[str, str]]:
    release = binary.parents[2] / "release"
    descriptor = os.open(release, os.O_RDONLY)
    try:
        assert os.read(descriptor, 1) == b"1"
    finally:
        os.close(descriptor)
    return [{"runner": "released"}]


def test_blocks_while_sibling_fails(binary: Path) -> list[dict[str, str]]:
    root = binary.parents[2]
    started = os.open(root / "started", os.O_WRONLY)
    try:
        assert os.write(started, b"1") == 1
    finally:
        os.close(started)
    return test_initializes_and_lists_tools(binary)


def test_fails_after_sibling_starts(binary: Path) -> list[dict[str, str]]:
    started = os.open(binary.parents[2] / "started", os.O_RDONLY)
    try:
        assert os.read(started, 1) == b"1"
    finally:
        os.close(started)
    raise AssertionError("fixture failure")


def test_blocks_before_queued_case(binary: Path) -> list[dict[str, str]]:
    test_initializes_and_lists_tools(binary)
    return [{"runner": "blocked"}]


def test_runs_after_blocked_case(binary: Path) -> list[dict[str, str]]:
    test_initializes_and_lists_tools(binary)
    return [{"runner": "queued"}]
