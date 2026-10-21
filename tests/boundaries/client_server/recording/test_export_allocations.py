"""Public export transfer bounds, measured in the Console process."""

import base64
import hashlib
import json
import mmap
import os
import struct
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.allocations import RetentionProfile
from support.assertions import last_result_text, wait_for_evaluation_output
from support.client import McpClient
from support.checkpoints import FifoCheckpoint
from support.execution import DIRECT, Execution, executions
from support.normalization import code
from support.native import LOADER_VARIABLE, build_interposer
from support.records import Transcript
from support.requirements import LINUX_NATIVE, NATIVE_FIXTURES, requires
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
                offset = 0
                for line in journal:
                    event = json.loads(line)
                    if event["event"] == "output_text":
                        assert event["offset"] == offset, event
                        assert event["discarded_bytes"] == 0, event
                        offset += event["bytes"]
            # The bounded recorder may coalesce adjacent observations. Every
            # byte still belongs to one contiguous retained range.
            assert offset == 131073, offset
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


@requires(NATIVE_FIXTURES)
@executions(DIRECT)
def test_bounds_export_of_large_documents(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        (workspace / "python3").symlink_to(sys.executable)
        with (
            closing(RetentionProfile(workspace)) as profile,
            closing(FifoCheckpoint.create(workspace / "export-entered")) as entered,
            closing(FifoCheckpoint.create(workspace / "export-release")) as release,
            McpClient(
                binary,
                execution.serve(),
                {**selected_environment(workspace), **profile.environment},
                current_directory=workspace,
            ) as client,
        ):
            client.initialize_and_list_tools()
            sources = []
            for index in range(48):
                source = (
                    f"# source {index:03} "
                    + "s" * (128 * 1024)
                    + f"\nprint('result {index:03}')"
                )
                sources.append(source)
                client.expect(f"result {index:03}\n", python=source)
            exporting = client.start_send(
                # fmt: python
                python=code("""
                    from pathlib import Path

                    with Path("export-entered").open("wb", buffering=0) as gate:
                        gate.write(b"1")
                    with Path("export-release").open("rb", buffering=0) as gate:
                        assert gate.read(1) == b"1"
                    paths = _console.export_session("report")
                    print("export complete")
                    """),
            )
            try:
                entered.wait("exporting cell reached its gate", client=client)
                # Cell entry no longer waits for journal projections. A later
                # recorded send settles that work before profiling export alone.
                inspection = client.send(requirements={"action": "get"})
                assert not inspection.get("isError"), inspection
                (recording,) = (workspace / ".agents/console/sessions").iterdir()
                assert (
                    'export_session("report")'
                    in (recording / "sessions/session-001.qmd").read_text()
                )
                profile.start()
            finally:
                release.release()
            client.receive(exporting)
            assert not exporting["result"]["isError"], exporting["result"]
            client.request("ping")
            peak = profile.stop()
            qmd = (workspace / "report/session.qmd").read_text()
            report = (workspace / "report/session.md").read_text()
            assert len(qmd) > 6 * 1024 * 1024 and len(report) > 6 * 1024 * 1024
            for index, source in enumerate(sources):
                assert source in qmd and source in report, index
                assert f"result {index:03}\n" in report, index
            assert "export_session" not in qmd and "export_session" not in report
            assert peak <= 4 * 1024 * 1024, peak
            client.finish()
            return [
                *client.transcript[:3],
                {"export memory": "source and reports spooled; transfer bounded"},
            ]


@requires(LINUX_NATIVE)
@executions(DIRECT)
def test_settled_calls_release_association_memory(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        heap = workspace / "heap"
        heap.write_bytes(bytes(8))
        with (
            heap.open("r+b") as stream,
            mmap.mmap(stream.fileno(), 8) as mapping,
            McpClient(
                binary,
                execution.serve("--worker", str(worker)),
                {
                    **os.environ,
                    LOADER_VARIABLE: str(
                        build_interposer(workspace, "association_allocations")
                    ),
                    "MCP_CONSOLE_TEST_ASSOCIATION_HEAP": str(heap),
                    # Measure live state rather than per-thread allocator caches.
                    "MALLOC_ARENA_MAX": "1",
                    "GLIBC_TUNABLES": "glibc.malloc.tcache_count=0",
                },
                current_directory=workspace,
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.send(r="echo ready")
            # Warm allocator/thread caches with an equal-sized batch before
            # checking that settled-call state stops growing.
            for _ in range(16384):
                client.send()
            client.request("ping")
            baseline = struct.unpack_from("=Q", mapping)[0]
            for arguments in ({}, {"requirements": {"action": "get"}}):
                for _ in range(16384):
                    result = client.send(**arguments)
                    assert not result.get("isError"), result
                client.request("ping")
                growth = struct.unpack_from("=Q", mapping)[0] - baseline
                assert growth <= 256 * 1024, growth
            client.finish()
            return [
                *client.transcript[:3],
                {
                    "association memory": "settled calls released while the worker survives"
                },
            ]


if __name__ == "__main__":
    run_this_suite(__file__)
