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
from support.assertions import last_result_text, wait_for_evaluation_output
from support.client import McpClient
from support.execution import DIRECT, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, requires
from support.suites import run_this_suite
from boundaries.client_server.python.test_without_r import selected_environment


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


@requires(NATIVE_FIXTURES)
@executions(DIRECT)
def test_bounds_export_of_fragmented_output(
    binary: Path, execution: Execution
) -> Transcript:
    with (
        tempfile.TemporaryDirectory() as directory,
        closing(RetentionProfile(Path(directory))) as profile,
    ):
        workspace = Path(directory)
        (workspace / "python3").symlink_to(sys.executable)
        with McpClient(
            binary,
            execution.serve(),
            {**selected_environment(workspace), **profile.environment},
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            previews: list[str] = []
            wait_for_evaluation_output(
                client,
                None,
                "fragmented output completion",
                output_cuts=previews,
                completion_timeout_seconds=client.response_timeout,
                timeout_ms=60_000,
                # fmt: python
                python=code(
                    """
                    import sys

                    counter = 1
                    for _ in range(131072):
                        sys.stdout.write("x")
                    print()
                    """
                ),
            )
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            with (recording / "capture/events.jsonl").open() as journal:
                chunks = sum(
                    json.loads(line)["event"] == "output_text" for line in journal
                )
            assert chunks >= 131072, chunks
            assert (recording / "capture/sessions/001/cell-001.txt").read_text() == (
                "x" * 131072 + "\n"
            )
            profile.start()
            client.expect(
                "1\n",
                python="paths = _console.export_session('report'); print(counter)",
            )
            client.request("ping")
            peak = profile.stop()
            report = (workspace / "report/session.md").read_text()
            qmd = (workspace / "report/session.qmd").read_text()
            assert all(preview in report for preview in previews), report
            assert "[output omitted:" in report, report
            assert "x" * 131072 not in report
            assert "counter = 1" in qmd and "export_session" not in qmd
            assert "export_session" not in report
            # The report is small even when capture contains many tiny chunks.
            # Reconstruct one event at a time, not a session-sized event vector.
            assert peak <= 4 * 1024 * 1024, peak
            client.finish()
            return [
                *client.transcript[:3],
                {
                    "export memory": "fragmented capture streamed; observed preview retained"
                },
            ]


if __name__ == "__main__":
    run_this_suite(__file__)
