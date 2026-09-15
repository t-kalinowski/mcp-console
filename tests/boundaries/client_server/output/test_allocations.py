#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.allocations import AllocationProfile
from support.client import McpClient
from support.execution import DIRECT
from support.previews import (
    assert_preview,
    compact_previews,
    normalize_preview_paths,
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
            compact_previews(client, "x", "ab")
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
            compact_previews(client, "x", "ab")
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
