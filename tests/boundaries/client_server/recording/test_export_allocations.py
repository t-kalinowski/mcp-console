"""Public export transfer bounds, measured in the Console process."""

import base64
import hashlib
import json
import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.allocations import RetentionProfile
from support.assertions import last_result_text
from support.client import McpClient
from support.execution import DIRECT, Execution, executions
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, requires
from support.suites import run_this_suite


@requires(NATIVE_FIXTURES)
@executions(DIRECT)
def test_bounds_image_transfer_per_artifact(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with (
        tempfile.TemporaryDirectory() as directory,
        closing(RetentionProfile(Path(directory))) as profile,
    ):
        with McpClient(
            binary,
            execution.serve("--worker", str(worker)),
            {**os.environ, **profile.environment},
        ) as client:
            client.initialize_and_list_tools()
            expected = hashlib.sha256()
            total = 0
            for _ in range(16):
                result = client.send(r="emit export image")
                (image,) = result["content"]
                assert image["type"] == "image" and image["mimeType"] == "image/png", (
                    result
                )
                data = base64.b64decode(image["data"], validate=True)
                assert data.startswith(b"\x89PNG\r\n\x1a\n"), data[:16]
                assert len(data) > 512 * 1024, len(data)
                expected.update(data)
                total += len(data)
                image["data"] = "<512 KiB PNG with a deterministic ancillary chunk>"
            profile.start()
            client.send(r="export image summary")
            summary = json.loads(last_result_text(client))
            client.request("ping")
            peak = profile.stop()
            assert summary == {
                "images": 16,
                "bytes": total,
                "sha256": expected.hexdigest(),
            }, summary
            # One artifact and its transfer copies fit below this bound; all
            # sixteen encoded images must never be simultaneously resident.
            assert peak <= 4 * 1024 * 1024, peak
            return client.finish()


@requires(NATIVE_FIXTURES)
@executions(DIRECT)
def test_export_does_not_retain_historical_stdin(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with (
        tempfile.TemporaryDirectory() as directory,
        closing(RetentionProfile(Path(directory))) as profile,
    ):
        with McpClient(
            binary,
            execution.serve("--worker", str(worker)),
            {**os.environ, **profile.environment},
        ) as client:
            client.initialize_and_list_tools()
            client.send(r="echo ready")
            for _ in range(16):
                client.send(stdin="x" * (1024 * 1024))
            client.send(control="restart")
            assert client.request("ping")["result"] == {}
            profile.start()
            client.send(r="export image summary")
            peak = profile.stop()
            assert json.loads(last_result_text(client))["images"] == 0
            assert peak <= 4 * 1024 * 1024, peak
            client.finish()
            return [
                *client.transcript[:3],
                {"export memory": "historical stdin omitted from source projection"},
            ]


if __name__ == "__main__":
    run_this_suite(__file__)
