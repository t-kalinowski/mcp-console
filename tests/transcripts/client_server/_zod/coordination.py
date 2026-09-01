import json
import os
import select
import tempfile
import time
from pathlib import Path
from typing import Self

from _support import McpClient


FIXTURE_CHECKPOINT_TIMEOUT_SECONDS = 15


TEST_EVENT_FIFO_NAME = "zod-test-events"


TEST_CONTROL_FIFO_NAME = "zod-test-control"


TEST_CLEANUP_FIFO_NAME = "zod-test-cleanup"


TEST_RESPONSE_QUERY_FIFO_NAME = "zod-test-response-query"


TEST_RESPONSE_RESULT_FIFO_NAME = "zod-test-response-result"


TEST_CONTROL_READY_NAME = "zod-test-control-ready"


class ZodFixtureControl:
    def __init__(self, root: Path | None = None) -> None:
        self.temporary_directory = (
            tempfile.TemporaryDirectory() if root is None else None
        )
        if root is None:
            assert self.temporary_directory is not None
            root = Path(self.temporary_directory.name)
        self.root = root
        self.event_reader: int | None = None
        self.control_writer: int | None = None
        self.cleanup_writer: int | None = None
        self.events: list[dict[str, object]] = []
        self.buffer = bytearray()
        self.cleanup_released = False

    def configure(self, environment: dict[str, str]) -> None:
        environment["TMPDIR"] = str(self.root)
        environment["ZOD_TEST_FIXTURE_CONTROL"] = "1"

    def connect(self, client: McpClient) -> None:
        if self.event_reader is not None:
            return
        directory = wait_for_marker(
            self.root,
            TEST_CONTROL_READY_NAME,
            client,
        ).parent
        event_reader = os.open(
            directory / TEST_EVENT_FIFO_NAME,
            os.O_RDONLY | os.O_NONBLOCK,
        )
        control_writer = os.open(
            directory / TEST_CONTROL_FIFO_NAME,
            os.O_WRONLY | os.O_NONBLOCK,
        )
        cleanup_writer = os.open(
            directory / TEST_CLEANUP_FIFO_NAME,
            os.O_WRONLY | os.O_NONBLOCK,
        )
        os.set_blocking(control_writer, True)
        os.set_blocking(cleanup_writer, True)
        self.event_reader = event_reader
        self.control_writer = control_writer
        self.cleanup_writer = cleanup_writer

    def send_control(self, operation: int, kind: str, **details: object) -> None:
        assert self.control_writer is not None
        payload = (
            json.dumps(
                {"operation": operation, "kind": kind, **details},
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        assert os.write(self.control_writer, payload) == len(payload)

    def release_cleanup(self) -> None:
        if self.cleanup_released:
            return
        if self.cleanup_writer is not None:
            try:
                os.write(self.cleanup_writer, b"1")
            except BrokenPipeError:
                pass
            os.close(self.cleanup_writer)
            self.cleanup_writer = None
        self.cleanup_released = True

    def wait_for(self, operation: int, kind: str) -> dict[str, object]:
        return self.wait_for_any(operation, {kind})

    def wait_for_any(
        self,
        operation: int,
        kinds: set[str],
    ) -> dict[str, object]:
        deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
        while True:
            event = next(
                (
                    event
                    for event in self.events
                    if event.get("operation") == operation
                    and event.get("kind") in kinds
                ),
                None,
            )
            if event is not None:
                return event
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"Zod did not emit one of {sorted(kinds)!r} for request "
                    f"{operation}; " + self.diagnostics()
                )
            assert self.event_reader is not None
            readable, _, _ = select.select([self.event_reader], [], [], remaining)
            if not readable:
                continue
            chunk = os.read(self.event_reader, 4096)
            assert chunk, "Zod event channel closed; " + self.diagnostics()
            self.record_events(chunk)

    def record_events(self, chunk: bytes) -> None:
        self.buffer.extend(chunk)
        while b"\n" in self.buffer:
            line, _, remainder = self.buffer.partition(b"\n")
            self.buffer = bytearray(remainder)
            event = json.loads(line)
            assert isinstance(event, dict), event
            assert set(event) >= {"operation", "kind", "component"}, event
            self.events.append(event)

    def assert_before(
        self,
        first: tuple[int, str],
        second: tuple[int, str],
    ) -> None:
        positions = {
            (event["operation"], event["kind"]): index
            for index, event in enumerate(self.events)
        }
        assert positions[first] < positions[second], self.diagnostics()

    def record_client_event(self, operation: int, kind: str, **details: object) -> None:
        self.events.append(
            {
                "operation": operation,
                "kind": kind,
                "component": "client",
                **details,
            }
        )

    def diagnostics(self) -> str:
        started = {
            event["operation"]
            for event in self.events
            if event.get("kind") == "worker_operation_started"
        }
        completed = {
            event["operation"]
            for event in self.events
            if event.get("kind") == "worker_operation_completed"
        }
        trace = "\n".join(json.dumps(event, sort_keys=True) for event in self.events)
        return f"outstanding requests: {sorted(started - completed)}; event trace:\n{trace}"

    def wait_for_eof(self) -> None:
        assert self.event_reader is not None
        deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    "Zod event channel remained open after fixture cleanup; "
                    + self.diagnostics()
                )
            readable, _, _ = select.select([self.event_reader], [], [], remaining)
            if not readable:
                continue
            chunk = os.read(self.event_reader, 4096)
            if not chunk:
                assert not self.buffer, self.diagnostics()
                return
            self.record_events(chunk)

    def close(self) -> None:
        self.release_cleanup()
        if self.control_writer is not None:
            os.close(self.control_writer)
        if self.event_reader is not None:
            os.close(self.event_reader)
        if self.temporary_directory is not None:
            self.temporary_directory.cleanup()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()


def wait_for_marker(root: Path, name: str, client: McpClient) -> Path:
    deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
    events = select.kqueue()
    directories: dict[Path, int] = {}
    try:
        events.control(
            [
                select.kevent(
                    client.process.pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_EXIT,
                )
            ],
            0,
            0,
        )
        while True:
            marker = find_marker(root, name)
            if marker is not None:
                return marker

            watch_marker_directories(root, events, directories)
            marker = find_marker(root, name)
            if marker is not None:
                return marker
            assert client.process.poll() is None, (
                f"mcp-console stopped before Zod reported its {name!r} checkpoint"
            )

            remaining = deadline - time.monotonic()
            assert remaining > 0, (
                f"Zod did not report its {name!r} checkpoint within "
                f"{FIXTURE_CHECKPOINT_TIMEOUT_SECONDS} seconds"
            )
            try:
                observed = events.control(
                    None,
                    max(1, len(directories) + 1),
                    remaining,
                )
            except InterruptedError:
                continue
            assert observed, (
                f"Zod did not report its {name!r} checkpoint within "
                f"{FIXTURE_CHECKPOINT_TIMEOUT_SECONDS} seconds"
            )
    finally:
        for descriptor in directories.values():
            os.close(descriptor)
        events.close()


def find_marker(root: Path, name: str) -> Path | None:
    markers = [path for path in [root / name] if path.exists()]
    markers.extend(root.glob(f"mcp-console-tmp-*/{name}"))
    assert len(markers) <= 1, f"found multiple {name} markers"
    return markers[0] if markers else None


def watch_marker_directories(
    root: Path,
    events: select.kqueue,
    directories: dict[Path, int],
) -> None:
    def watch(directory: Path) -> None:
        if directory in directories:
            return
        try:
            descriptor = os.open(directory, os.O_EVTONLY | os.O_CLOEXEC)
        except FileNotFoundError:
            return
        events.control(
            [
                select.kevent(
                    descriptor,
                    filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=(
                        select.KQ_NOTE_WRITE
                        | select.KQ_NOTE_RENAME
                        | select.KQ_NOTE_DELETE
                        | select.KQ_NOTE_REVOKE
                    ),
                )
            ],
            0,
            0,
        )
        directories[directory] = descriptor

    # Watch the root before discovering private temporary directories. This
    # makes creation of a directory after the glob snapshot observable.
    watch(root)
    for directory in root.glob("mcp-console-tmp-*"):
        if directory.is_dir():
            watch(directory)


def release_partial_sideband(marker: Path) -> None:
    release = marker.with_name("zod-release-partial-sideband")
    with release.open("wb", buffering=0) as stream:
        assert stream.write(b"x") == 1


def release_fixture_checkpoint(path: Path) -> None:
    with path.open("wb", buffering=0) as stream:
        assert stream.write(b"1") == 1
