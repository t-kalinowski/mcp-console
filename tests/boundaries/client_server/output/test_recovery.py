#!/usr/bin/env -S uv run --script

import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.allocations import AllocationProfile
from support.assertions import last_tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT
from support.previews import (
    assert_preview,
    compact_previews,
    cell_text,
    normalize_preview_paths,
    session_directory,
)
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, requires
from support.suites import run_this_suite


@requires(NATIVE_FIXTURES)
def test_cancelled_control_recovery_keeps_bounded_allocations(
    binary: Path,
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with (
            closing(FifoCheckpoint.create(root / "result-reached")) as reached,
            closing(FifoCheckpoint.create(root / "result-release")) as release,
            closing(AllocationProfile(root)) as profile,
            McpClient(
                binary,
                DIRECT.serve("--worker", str(worker)),
                {
                    **os.environ,
                    **profile.environment,
                    "MCP_CONSOLE_TEST_RESULT_REACHED": str(reached.path),
                    "MCP_CONSOLE_TEST_RESULT_RELEASE": str(release.path),
                },
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.send(r="echo ready")
            profile.start()
            profile.pause_results(True)
            cancellation_start = len(client.transcript)
            try:
                for _ in range(1024):
                    pending = client.start_send(control="interrupt")
                    reached.wait("controlled result owns delivery before journaling")
                    client.notify("notifications/cancelled", requestId=pending["id"])
                    # Receipt of ping follows cancellation on the same MCP input.
                    assert client.request("ping")["result"] == {}
                    release.release()
                    assert "result" not in pending, pending
            finally:
                profile.pause_results(False)
                release.release()
            compact_cancelled_exchanges(client, cancellation_start, count=1024)
            result = client.send(control="interrupt")
            client.request("ping")
            _, largest = profile.stop()
            # Allow the retained control notices and temporary vector growth,
            # but not an additional history of invisible source receipts.
            assert largest <= 256 * 1024, largest
            assert not result["isError"], result
            text = result["content"][0]["text"]
            assert len(text.encode()) <= 8192
            assert text == "\n[idle]" * 1025
            client.send()
            assert last_tool_text(client) == "\n[idle]"
            compact_previews(
                client,
                "\n[idle]",
                "\n[worker stopped: in-memory state lost]\n[starting new worker]\n[done]",
                "x",
            )
            return client.finish()


@requires(NATIVE_FIXTURES)
def test_recovered_recorded_cells_keep_bounded_source_markers(
    binary: Path,
) -> Transcript:
    return recovered_recorded_cells(binary, count=32, silent=False)


@requires(NATIVE_FIXTURES)
def test_recovered_silent_cells_discard_file_receipts(binary: Path) -> Transcript:
    return recovered_recorded_cells(binary, count=512, silent=True)


def recovered_recorded_cells(binary: Path, *, count: int, silent: bool) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with (
            closing(FifoCheckpoint.create(root / "result-reached")) as reached,
            closing(FifoCheckpoint.create(root / "result-release")) as release,
            closing(AllocationProfile(root)) as profile,
            McpClient(
                binary,
                DIRECT.serve("--worker", str(worker)),
                {
                    **os.environ,
                    **profile.environment,
                    "MCP_CONSOLE_TEST_RESULT_REACHED": str(reached.path),
                    "MCP_CONSOLE_TEST_RESULT_RELEASE": str(release.path),
                },
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.send(r="echo ready")
            profile.start()
            profile.pause_results(True)
            cancellation_start = len(client.transcript)
            try:
                for index in range(count):
                    pending = client.start_send(
                        control="restart",
                        r="complete silently"
                        if silent
                        else f"preview recovery cell {index}",
                    )
                    reached.wait(
                        "replacement cell result owns delivery before journaling"
                    )
                    client.notify("notifications/cancelled", requestId=pending["id"])
                    assert client.request("ping")["result"] == {}
                    release.release()
                    assert "result" not in pending, pending
            finally:
                profile.pause_results(False)
                release.release()
            compact_cancelled_exchanges(
                client,
                cancellation_start,
                count=count,
                source_template=None if silent else "preview recovery cell {index}",
            )
            result = client.send(control="interrupt")
            client.request("ping")
            _, largest = profile.stop()
            if silent:
                # Control history is already bounded independently of cell text. Repeated
                # empty files must not add another unbounded receipt history.
                assert largest <= 64 * 1024, largest
            assert not result["isError"], result
            text = result["content"][0]["text"]
            assert len(text.encode()) <= 8192
            assert text.endswith("[done]"), repr(text[-200:])
            events = [
                json.loads(line)
                for line in (session_directory(client) / "internal/events.jsonl")
                .read_text()
                .splitlines()
            ]
            summaries = {
                event["call_id"]: event
                for event in events
                if event["event"] == "cell_output"
            }
            assert len(summaries) == count + 1, summaries.keys()
            if silent:
                assert "output preview" not in text
            else:
                assert "cell 0 head\n" in text and f"cell {count - 1} tail\n" in text
                assert "outputs/call-000002.log" in text
                assert f"outputs/call-{count + 1:06}.log" in text
                assert "internal/events.jsonl" in text, {
                    "head": text[:200],
                    "tail": text[-1000:],
                    "markers": re.findall(r"\[output preview:[^\n]*", text),
                }
                omitted = sum(
                    map(int, re.findall(r"output preview: omitted (\d+)", text))
                )
                assert (
                    sum(s["inline_omitted_bytes"] for s in summaries.values())
                    == omitted
                )
            for index in range(count):
                emitted = (
                    ""
                    if silent
                    else f"cell {index} head\n" + "x" * 32768 + f"\ncell {index} tail\n"
                )
                assert cell_text(client, index + 2) == emitted
                summary = summaries[index + 2]
                assert summary["retained_bytes"] == len(emitted.encode())
                assert summary["discarded_bytes"] == 0
                if silent:
                    assert summary["inline_omitted_bytes"] == 0
                elif 0 < index < count - 1:
                    assert summary["inline_omitted_bytes"] == len(emitted.encode())
            client.send()
            assert last_tool_text(client) == "\n[idle]"
            normalize_preview_paths(client)
            compact_previews(
                client,
                "\n[idle]",
                "\n[worker stopped: in-memory state lost]\n[starting new worker]\n[done]",
                "x",
            )
            return client.finish()


def compact_cancelled_exchanges(
    client: McpClient, start: int, *, count: int, source_template: str | None = None
) -> None:
    # Verify every repeated setup exchange before recording one representative.
    # The full response is checked before any lossless repetition notation.
    entries = client.transcript[start:]
    assert len(entries) == 3 * count
    for index, (pending, cancelled, ping) in enumerate(
        zip(entries[::3], entries[1::3], entries[2::3], strict=True)
    ):
        expected = dict(entries[0]["send"])
        if source_template is not None:
            expected["r"] = source_template.format(index=index)
        assert pending == {"id": pending["id"], "send": expected}
        assert cancelled == {
            "input": {
                "method": "notifications/cancelled",
                "params": {"requestId": pending["id"]},
            }
        }
        assert ping == {"id": ping["id"], "input": {"method": "ping"}, "result": {}}
    entries[0]["transcript_normalization"] = {
        "target": "repeated cancellation setup exchanges",
        "repeated_exchange_count": count,
        "exchange_entry_count": 3,
        "request_ids": "distinct in each exchange",
    }
    if source_template is not None:
        entries[0]["transcript_normalization"]["send.r"] = {
            "template": source_template,
            "index_start": 0,
            "index_count": count,
        }
    client.transcript[start:] = entries[:3]


@contextmanager
def recovery_client(
    binary: Path,
) -> Iterator[tuple[McpClient, AllocationProfile, FifoCheckpoint, FifoCheckpoint]]:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with (
            closing(FifoCheckpoint.create(root / "result-reached")) as reached,
            closing(FifoCheckpoint.create(root / "result-release")) as release,
            closing(AllocationProfile(root)) as profile,
            McpClient(
                binary,
                DIRECT.serve("--worker", str(worker)),
                {
                    **os.environ,
                    **profile.environment,
                    "MCP_CONSOLE_TEST_RESULT_REACHED": str(reached.path),
                    "MCP_CONSOLE_TEST_RESULT_RELEASE": str(release.path),
                    "MCP_CONSOLE_TEST_PREVIEW_DIRECTORY": str(root),
                },
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.send(r="echo ready")
            profile.pause_results(True)
            try:
                yield client, profile, reached, release
            finally:
                profile.pause_results(False)
                release.release()


def cancel_result(
    client: McpClient,
    pending: dict,
    reached: FifoCheckpoint,
    release: FifoCheckpoint,
) -> None:
    reached.wait("result owns delivery before journaling")
    client.notify("notifications/cancelled", requestId=pending["id"])
    assert client.request("ping")["result"] == {}
    release.release()
    assert "result" not in pending, pending


@requires(NATIVE_FIXTURES)
def test_cancelled_active_polls_replay_before_later_output(binary: Path) -> Transcript:
    with recovery_client(binary) as (client, profile, reached, release):
        root = reached.path.parent
        with (
            closing(FifoCheckpoint.create(root / "preview-release")) as worker_release,
            closing(FifoCheckpoint.create(root / "preview-processed")) as processed,
        ):
            try:
                profile.pause_results(False)
                client.send(r="preview recovery intervals", timeout_ms=0)
                assert last_tool_text(client) == "\n[running; poll with an empty send]"
                profile.pause_results(True)
                emitted = []
                for index in range(3):
                    worker_release.release()
                    processed.wait("active cell interval reached the output tape")
                    emitted.append(
                        f"interval {index} head\n"
                        + "x" * 32768
                        + f"\ninterval {index} tail\n"
                    )
                    if index < 2:
                        pending = client.start_send(timeout_ms=0)
                        cancel_result(client, pending, reached, release)
                profile.pause_results(False)
                result = client.send(timeout_ms=0)
                assert not result["isError"], result
                recorded = next(
                    event["result"]
                    for line in (session_directory(client) / "internal/events.jsonl")
                    .read_text()
                    .splitlines()
                    if (event := json.loads(line))["event"] == "tool_result"
                    and event["call_id"] == 3
                )
                assert result["content"] == recorded["content"]
                assert result["isError"] == recorded["isError"]
                text = result["content"][0]["text"]
                state = "\n[running; poll with an empty send]"
                assert text.endswith(state)
                assert_preview(text.removesuffix(state), emitted[0])
                assert "internal/events.jsonl" not in text
                assert "outputs/call-000002.log" in text
                assert cell_text(client, 2) == "".join(emitted)
                client.send(timeout_ms=0)
                later = last_tool_text(client)
                assert later.endswith(state)
                assert_preview(later.removesuffix(state), "".join(emitted[1:]))
                assert "outputs/call-000002.log" in later
                worker_release.release()
                client.send()
                assert last_tool_text(client) == "[done]"
                client.send()
                assert last_tool_text(client) == "\n[idle]"
                normalize_preview_paths(client)
                compact_previews(
                    client,
                    "\n[idle]",
                    "\n[worker stopped: in-memory state lost]\n[starting new worker]\n[done]",
                    "x",
                )
                return client.finish()
            finally:
                for _ in range(4):
                    worker_release.release()


@requires(NATIVE_FIXTURES)
def test_image_recovery_moves_the_retained_preview(binary: Path) -> Transcript:
    with recovery_client(binary) as (client, profile, reached, release):
        pending = client.start_send(r="preview allocation image")
        cancel_result(client, pending, reached, release)
        # Stop at the next result journal write: image ingestion and persistence
        # have finished, and MCP serialization has not started yet.
        profile.start()
        recovered = client.start_send()
        reached.wait("recovered image owns delivery before journaling")
        allocated, _ = profile.stop()
        release.release()
        client.receive(recovered)
        result = recovered["result"]
        assert result == {
            "content": [
                {
                    "type": "image",
                    "data": "A" * (8 * 1024 * 1024),
                    "mimeType": "image/png",
                }
            ],
            "isError": False,
        }
        session = session_directory(client)
        events = [
            json.loads(line)
            for line in (session / "internal/events.jsonl").read_text().splitlines()
        ]
        artifacts = [event for event in events if event["event"] == "artifact_created"]
        assert len(artifacts) == 1, artifacts
        artifact = artifacts[0]
        assert artifact["mime_type"] == "image/png"
        assert artifact["bytes"] == 6 * 1024 * 1024
        assert (session / artifact["path"]).read_bytes() == bytes(6 * 1024 * 1024)
        recorded = [
            event["result"]["content"]
            for event in events
            if event["event"] == "tool_result" and event["call_id"] in (2, 3)
        ]
        assert (
            recorded
            == [
                [
                    {
                        "type": "image",
                        "artifactId": artifact["artifact_id"],
                        "path": artifact["path"],
                        "mimeType": "image/png",
                    }
                ]
            ]
            * 2
        ), recorded
        # One encoded copy is required for MCP; retaining recovery must move the
        # existing preview. Allow small response, journal, and transport overhead.
        assert allocated < 9 * 1024 * 1024, allocated
        result["content"][0]["data"] = "<image byte-identical to 6 MiB of zero bytes>"
        profile.pause_results(False)
        client.send()
        assert last_tool_text(client) == "\n[idle]"
        return client.finish()


@requires(NATIVE_FIXTURES)
def test_recovered_image_omissions_keep_bounded_state(binary: Path) -> Transcript:
    count = 1024
    with recovery_client(binary) as (client, profile, reached, release):
        start = len(client.transcript)
        for _ in range(count):
            pending = client.start_send(control="restart", r="preview rejected image")
            cancel_result(client, pending, reached, release)
        compact_cancelled_exchanges(client, start, count=count)
        profile.pause_results(False)
        # Measure recovery after image parsing and admission have completed.
        profile.start()
        result = client.send(control="interrupt")
        client.request("ping")
        _, largest = profile.stop()
        assert largest <= 128 * 1024, largest
        assert not result["isError"], result
        text = result["content"][0]["text"]
        assert text.count("[image limit:") == 1
        assert (
            f"omitted {count} images ({4 * count} encoded bytes); "
            f"0 already recorded, {count} not retained"
        ) in text
        client.send()
        assert last_tool_text(client) == "\n[idle]"
        compact_previews(
            client,
            "\n[worker stopped: in-memory state lost]\n[starting new worker]\n[done]",
        )
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
