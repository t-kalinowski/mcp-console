"""Portable session documents and their automatically announced locations."""

import base64
import os
import re
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import normalize_recording
from support.records import Transcript
from support.suites import run_this_suite
from support.requirements import R, SQL, POSIX, requires
from support.normalization import code
from support.checkpoints import release_fixture_checkpoint
from boundaries.client_server._harness import wait_for_marker
from boundaries.client_server.python.test_without_r import selected_environment


@executions(DIRECT, SANDBOXED)
def test_materializes_portable_bundle(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary, execution.serve(), current_directory=workspace
        ) as client:
            client.initialize_and_list_tools()
            client.request("ping")
            root = workspace / ".agents/console/sessions"
            assert not root.exists() or not list(root.iterdir())
            response = client.send(
                # fmt: python
                python=code("""
                    executions = globals().get("executions", 0) + 1
                    identity = object()
                    print("observed value")
                    """)
            )
            (recording,) = root.iterdir()
            bundle = recording / "sessions/session-001"
            assert (bundle / "session.qmd").is_file()
            assert (bundle / "session.md").is_file()
            notice = client.bundle_notices[-1]
            assert str(bundle) in notice, response
            assert "Console host" in notice, notice
            assert "observed value" in (bundle / "session.md").read_text()
            qmd = (bundle / "session.qmd").read_text()
            assert "identity = object()" in qmd, qmd
            assert "ir render session.qmd" in qmd, qmd
            assert qmd.count("observed value") == 1, (
                qmd
            )  # Source without captured output.
            client.send(
                python="assert identity is not None and executions == 1; raise ValueError('kept error')"
            )
            report = (bundle / "session.md").read_text()
            assert "ValueError: kept error" in report, report
            assert "Session bundle" not in report, report
            client.send(python="assert not hasattr(_console, 'export_session')")
            transcript = client.finish()
            copied = workspace / "copied"
            shutil.copytree(bundle, copied)
            assert (copied / "session.md").read_text() == (
                bundle / "session.md"
            ).read_text()
            events = [
                json.loads(line)
                for line in (recording / "capture/events.jsonl")
                .read_text()
                .splitlines()
            ]
            assert any(event["event"] == "recording_ended" for event in events)
            return normalize_recording(transcript, workspace)


@executions(DIRECT, SANDBOXED)
def test_python_bundle_without_r_and_replacement(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        if os.name != "nt":
            (root / "python3").symlink_to(sys.executable)
        with McpClient(
            binary,
            execution.serve(),
            selected_environment(root),
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("original\n", python="print('original')")
            client.send(control="restart")
            assert len(client.bundle_notices) == 1, client.bundle_notices
            client.expect("replacement\n", python="print('replacement')")
            assert len(client.bundle_notices) == 2, client.bundle_notices
            (recording,) = (root / ".agents/console/sessions").iterdir()
            for id, label in ((1, "original"), (2, "replacement")):
                bundle = recording / f"sessions/session-{id:03}"
                qmd = (bundle / "session.qmd").read_text()
                report = (bundle / "session.md").read_text()
                assert f"print('{label}')" in qmd and f"print('{label}')" in report
                assert "r-available: false" in qmd, qmd
                assert f"{label}\n```" in report, report
                assert ("replacement" if id == 1 else "original") not in report
            return normalize_recording(client.finish(), root)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_images_are_portable_and_later_polls_keep_intervals(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with McpClient(
            binary,
            execution.serve("--worker", str(worker)),
            {**os.environ, "TMPDIR": directory, "ZOD_RECORDING_INTERVALS": "1"},
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            client.send(r="record intervals")
            release_fixture_checkpoint(
                wait_for_marker(root, "zod-recording-post-release", client),
                client=client,
            )
            wait_for_marker(root, "zod-recording-post-observed", client)
            client.send()
            formats = client.send(r="emit image formats")
            transcript = client.finish()
        (recording,) = (root / ".agents/console/sessions").iterdir()
        bundle = recording / "sessions/session-001"
        report = (bundle / "session.md").read_text()
        assert "Startup output" in report and "Between cells (after 001)" in report, (
            report
        )
        capture = [
            json.loads(line)
            for line in (recording / "capture/events.jsonl").read_text().splitlines()
        ]
        for result in capture:
            if result["event"] == "tool_result" and result["call_id"] in (1, 2):
                for part in result["parts"]:
                    block = result["result"]["content"][part["block"]]
                    if block["type"] == "text" and part["owner"] is not None:
                        fragment = (
                            block["text"].encode()[part["start"] : part["end"]].decode()
                        )
                        assert fragment in report, (fragment, report)
        links = re.findall(r"!\[Recorded image\]\(<(images/[^>]+)>\)", report)
        assert links and len(links) == len(set(links)), report
        formats_images = [
            base64.b64decode(block["data"])
            for block in formats["content"]
            if block["type"] == "image"
        ]
        retained = [(bundle / link).read_bytes() for link in links]
        assert all(image in retained for image in formats_images)
        assert any(link.endswith(".jpg") for link in links), links
        assert "![" not in (bundle / "session.qmd").read_text()
        copy = root / "copy"
        shutil.copytree(bundle, copy)
        transcript = normalize_recording(transcript, root)
        shutil.rmtree(recording)
        assert (copy / "session.md").read_text() == report
        assert all((copy / link).is_file() for link in links)
        return normalize_recording(transcript, root)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_r_report_does_not_initialize_python_or_replay(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with McpClient(binary, execution.serve(), current_directory=root) as client:
            client.initialize_and_list_tools()
            client.expect(
                "recorded once\n",
                # fmt: r
                r=code("""
                    executions <- if (exists("executions", inherits = FALSE)) {
                      executions + 1L
                    } else {
                      1L
                    }
                    stopifnot(!"export_session" %in% names(.console))
                    cat("recorded once\\n")
                    """),
            )
            (recording,) = (root / ".agents/console/sessions").iterdir()
            bundle = recording / "sessions/session-001"
            assert "recorded once\n```" in (bundle / "session.md").read_text()
            client.expect(
                r="stopifnot(executions == 1L, !reticulate::py_available(initialize = FALSE))"
            )
            return normalize_recording(client.finish(), root)


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_sql_report_preserves_preview_without_repeating_query(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with McpClient(binary, execution.serve(), current_directory=root) as client:
            client.initialize_and_list_tools()
            client.send(
                sql="CREATE SEQUENCE IF NOT EXISTS execution_count START 1; SELECT nextval('execution_count') AS value"
            )
            (recording,) = (root / ".agents/console/sessions").iterdir()
            report = (recording / "sessions/session-001/session.md").read_text()
            assert "execution_count" in report and "value" in report, report
            client.send(
                sql="SELECT currval('execution_count') AS still_one",
                requirements={"duckdb": ["fts"]},
            )
            output = client.transcript[-1]["result"]["content"][0]["text"]
            assert re.search(r"(?m)^1\s+1$", output), output
            qmd = (recording / "sessions/session-001/session.qmd").read_text()
            assert "    - fts\n" in qmd, qmd
            return normalize_recording(client.finish(), root)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_bundle_image_failure_preserves_capture(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with McpClient(
            binary, execution.serve("--worker", str(worker)), current_directory=root
        ) as client:
            client.initialize_and_list_tools()
            client.expect("zod: ready\n", r="echo ready")
            (recording,) = (root / ".agents/console/sessions").iterdir()
            bundle = recording / "sessions/session-001"
            (bundle / "images/image-000001.png").mkdir(parents=True)
            result = client.send(r="emit image")
            assert any(block["type"] == "image" for block in result["content"]), result
            client.expect("zod: still running\n", r="echo still running")
            transcript, stderr = client.finish_with_standard_error()
        capture = [
            json.loads(line)
            for line in (recording / "capture/events.jsonl").read_text().splitlines()
        ]
        assert capture[-1]["event"] == "recording_ended", capture[-1]
        assert any(event["event"] == "image_retained" for event in capture), capture
        assert "recording disabled" not in stderr, stderr
        assert "projections disabled" in stderr, stderr
        transcript.append(
            {
                "bundle image failure": "projection disabled; capture and computation completed"
            }
        )
        return normalize_recording(transcript, root)


@executions(DIRECT, SANDBOXED)
def test_discovery_preserves_status_suffix_and_polled_results(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with McpClient(binary, execution.serve(), current_directory=root) as client:
            client.initialize_and_list_tools()
            client.send(python="answer = input('answer> '); print(answer)")
            (recording,) = (root / ".agents/console/sessions").iterdir()
            capture = [
                json.loads(line)
                for line in (recording / "capture/events.jsonl")
                .read_text()
                .splitlines()
            ]
            result = next(
                event["result"]
                for event in reversed(capture)
                if event["event"] == "tool_result"
            )
            assert result["content"][0]["_meta"] == {
                "mcp-console/session-bundle": True
            }, result
            combined = "".join(
                block["text"] for block in result["content"] if block["type"] == "text"
            )
            assert combined.endswith("[waiting for stdin]"), combined
            client.expect("accepted\n", stdin="accepted\n")
            report = (recording / "sessions/session-001/session.md").read_text()
            assert "answer> " in report and "accepted\n```" in report, report
            assert report.count("## Cell 001") == 1 and "## Cell 002" not in report
            assert "_Execution ended; completion confirmed._" in report, report
            assert len(client.bundle_notices) == 1, client.bundle_notices
            return normalize_recording(client.finish(), root)


@executions(DIRECT, SANDBOXED)
def test_report_keeps_rejected_dependency_outcome(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with McpClient(binary, execution.serve(), current_directory=root) as client:
            client.initialize_and_list_tools()
            client.expect("ready\n", python="print('ready')")
            rejected = client.send(requirements={})
            assert rejected["isError"], rejected
            (recording,) = (root / ".agents/console/sessions").iterdir()
            report = (recording / "sessions/session-001/session.md").read_text()
            assert rejected["content"][0]["text"] in report, (rejected, report)
            return normalize_recording(client.finish(), root)


if __name__ == "__main__":
    run_this_suite(__file__)
