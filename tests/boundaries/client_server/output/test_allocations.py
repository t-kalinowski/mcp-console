#!/usr/bin/env -S uv run --script

import json
import os
import re
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.allocations import AllocationProfile
from support.assertions import last_tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT
from support.previews import (
    assert_preview,
    cell_text,
    normalize_preview_paths,
    session_directory,
)
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, requires
from support.suites import run_this_suite


@requires(NATIVE_FIXTURES)
def test_tiny_events_have_bounded_allocation_work(binary: Path) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with (
            closing(AllocationProfile(root)) as profile,
            McpClient(
                binary,
                DIRECT.serve("--worker", str(worker)),
                {**os.environ, **profile.environment},
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.send(r="echo ready")
            profile.start()
            result = client.send(r="preview many tiny events", timeout_ms=600_000)
            client.request("ping")
            allocated, _ = profile.stop()
            assert allocated < 512 * 1024 * 1024, allocated
            assert not result["isError"], result
            assert [part["type"] for part in result["content"]] == [
                "text",
                "image",
                "text",
            ]
            assert_preview(
                result["content"][0]["text"],
                "preview head\n" + "ab" * 100000 + "\npreview tail: final diagnostic\n",
            )
            assert result["content"][-1]["text"] == "after final image\n"
            normalize_preview_paths(client)
            return client.finish()


@requires(NATIVE_FIXTURES)
def test_text_sizing_does_not_copy_images(binary: Path) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with (
            closing(AllocationProfile(root)) as profile,
            McpClient(
                binary,
                DIRECT.serve("--worker", str(worker)),
                {**os.environ, **profile.environment},
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.send(r="echo ready")
            allocations = []
            for index, scenario in enumerate(
                (
                    "preview allocation image",
                    "preview allocation image",
                    "preview allocation image and text",
                )
            ):
                profile.start()
                result = client.send(r=scenario)
                client.request("ping")
                allocated, _ = profile.stop()
                # Warm the transport's image-sized buffers before comparison.
                if index:
                    allocations.append(allocated)
                assert not result["isError"], result
                image = result["content"][0]
                assert image == {
                    "type": "image",
                    "data": "A" * (8 * 1024 * 1024),
                    "mimeType": "image/png",
                }
                image["data"] = "<image byte-identical to 6 MiB of zero bytes>"
            # Adding 32 KiB of text may allocate its bounded preview and notices,
            # but must not add copies proportional to the unrelated 8 MiB image.
            assert allocations[1] < allocations[0] + 2 * 1024 * 1024, allocations
            assert_preview(
                result["content"][1]["text"],
                "preview head\n" + "x" * 32768 + "\npreview tail\n",
            )
            normalize_preview_paths(client)
            return client.finish()


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
            result = client.send(control="interrupt")
            client.request("ping")
            _, largest = profile.stop()
            if silent:
                # Control history is already summarized within 8 KiB. Repeated
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
                assert "internal/events.jsonl" in text
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
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
