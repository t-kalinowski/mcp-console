"""Recording and export contracts observed through MCP and runtime helpers."""

import base64
import json
import shutil
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.normalization import code
from support.progress import normalize_elapsed
from boundaries.client_server._harness import wait_for_marker
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.assertions import last_result_text
from support.requirements import POSIX, R, SQL, requires
from support.records import Transcript
from support.suites import run_this_suite
from boundaries.client_server.python.test_without_r import selected_environment


def events(recording: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (recording / "capture/events.jsonl").read_text().splitlines()
    ]


def normalized(transcript: Transcript, workspace: Path) -> Transcript:
    """Normalize locations and timing only after public content assertions."""
    for entry in transcript:
        for block in entry.get("result", {}).get("content", []):
            if block["type"] == "text":
                block["text"] = normalize_elapsed(block["text"])
    data = json.dumps(transcript)
    recordings = workspace / ".agents/console/sessions"
    if recordings.is_dir():
        for recording in recordings.iterdir():
            data = data.replace(recording.name, "<recording ID>")
    for path in [workspace, workspace.resolve()]:
        data = data.replace(json.dumps(str(path))[1:-1], "<workspace>")
    return json.loads(data)


@executions(DIRECT, SANDBOXED)
def test_python_export_without_r(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        if os.name != "nt":
            (workspace / "python3").symlink_to(sys.executable)
        env = selected_environment(workspace)
        env["RETICULATE_PYTHON"] = sys.executable
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {"startup": {"language": "python", "code": "print('startup result')"}}
            )
        )
        with McpClient(
            binary, execution.serve(), env, current_directory=workspace
        ) as client:
            client.initialize_and_list_tools()
            client.request("ping")
            assert not (workspace / ".agents/console/sessions").exists()
            client.send(requirements={"action": "get"})
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            prelude = events(recording)
            incoming = [
                event["message"].get("method")
                for event in prelude
                if event["event"] == "protocol" and event["direction"] == "received"
            ]
            assert incoming[:4] == [
                "initialize",
                "notifications/initialized",
                "tools/list",
                "ping",
            ], incoming
            client.send(python="counter = 1; print('original result')")
            client.send(python="raise ValueError('retained failure')")
            client.send(python="print('x' * 20000)")
            preview = last_result_text(client)
            client.send(
                python="paths = _console.export_session('report'); print(counter)"
            )
            assert "1" in last_result_text(client)
            report = workspace / "report"
            qmd = (report / "session.qmd").read_text()
            md = (report / "session.md").read_text()
            assert "counter = 1" in qmd and "retained failure" in qmd
            assert "export_session" not in qmd and "export_session" not in md
            assert "original result" in md and "ValueError: retained failure" in md
            assert md.index("## Startup output") < md.index("## Cell 001")
            assert "startup result" in md
            for part in preview.split("\n[done]"):
                assert part in md
            assert "x" * 20000 not in md
            assert not (recording / "transcript.qmd").exists()
            assert (recording / "sessions/session-001.qmd").is_file()
            client.send(python="_console.export_session('report')")
            assert "exist" in last_result_text(client).lower()
            assert (report / "session.md").read_text() == md
            client.send(control="restart", python="print('replacement result')")
            client.send(python="paths = _console.export_session('replacement')")
            replacement = (workspace / "replacement/session.md").read_text()
            assert (
                "replacement result" in replacement
                and "original result" not in replacement
            )
            client.send(python="import os; os._exit(17)", timeout_ms=60000)
            client.send(python="print('automatic replacement')")
            client.send(python="paths = _console.export_session('automatic')")
            automatic = (workspace / "automatic/session.md").read_text()
            assert (
                "automatic replacement" in automatic
                and "replacement result" not in automatic
            )
            transcript = client.finish()
        assert (
            len([e for e in events(recording) if e["event"] == "worker_started"]) == 3
        )
        for path in recording.rglob("*"):
            if path.is_file():
                assert not path.stat().st_mode & 0o222, path
        for path in report.rglob("*"):
            if path.is_file():
                assert path.stat().st_mode & 0o200, path
        return normalized(transcript, workspace)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_r_export_without_python_initialization(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary, execution.serve(), current_directory=workspace
        ) as client:
            client.initialize_and_list_tools()
            client.send(r='counter <- 1L; cat("original R result\\n")')
            client.send(
                r='paths <- .console$export_session("report"); stopifnot(counter == 1L, !reticulate::py_available(initialize = FALSE))'
            )
            qmd = (workspace / "report/session.qmd").read_text()
            md = (workspace / "report/session.md").read_text()
            assert "counter <- 1L" in qmd and "export_session" not in qmd
            assert "original R result" in md
            return normalized(client.finish(), workspace)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_cell_intervals_and_empty_cells(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            {**os.environ, "TMPDIR": directory, "ZOD_RECORDING_INTERVALS": "1"},
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(r="record intervals")
            wait_for_marker(workspace, "zod-recording-post-observed", client)
            (active_recording,) = (workspace / ".agents/console/sessions").iterdir()
            active_capture = active_recording / "capture/sessions/001"
            assert (active_capture / "cell-001.txt").stat().st_mode & 0o222 == 0
            assert (active_capture / "cell-001-post.txt").stat().st_mode & 0o200
            assert (active_recording / "capture/events.jsonl").stat().st_mode & 0o200
            client.send(r="complete silently")
            assert (active_capture / "cell-001-post.txt").stat().st_mode & 0o222 == 0
            client.request(
                "tools/call",
                name="send",
                arguments={"r": "rejected source", "typo": True},
            )
            client.send(control="restart", r="complete silently")
            restart = [
                event
                for event in events(active_recording)
                if event["event"] == "tool_result"
            ][-1]
            assert {
                part["owner"]["session_id"]
                for part in restart["parts"]
                if part["owner"] is not None
            } == {1, 2}, restart
            assert (
                active_recording / "sessions/session-001.qmd"
            ).stat().st_mode & 0o222 == 0
            transcript = client.finish()
        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        capture = recording / "capture/sessions/001"
        assert (capture / "startup.txt").read_text() == "startup text\n"
        assert (capture / "cell-001.txt").read_text() == "before image\nafter image\n"
        assert (capture / "cell-001-post.txt").read_text() == "between cells\n"
        assert (capture / "cell-002-post.txt").read_text() == "shutdown text\n"
        for name in [
            "startup-001.png",
            "cell-001-002.png",
            "cell-001-post-001.png",
            "cell-002-post-001.png",
        ]:
            assert (capture / name).is_file()
        interleaving = []
        for event in events(recording):
            if event["event"] not in {"output_text", "artifact_created"} or event[
                "owner"
            ] != {"session_id": 1, "cell_id": 1, "interval": "cell"}:
                continue
            if event["event"] == "output_text":
                raw = (recording / event["path"]).read_bytes()
                interleaving.append(
                    raw[event["offset"] : event["offset"] + event["bytes"]].decode()
                )
            else:
                interleaving.append("image")
        assert interleaving == ["before image\n", "image", "after image\n", "image"]
        assert (capture / "cell-001-001.png").is_file()
        assert (capture / "cell-002.txt").read_bytes() == b""
        assert not (capture / "cell-003.txt").exists()
        assert (recording / "capture/sessions/002/cell-001.txt").read_bytes() == b""
        assert "rejected source" in (recording / "sessions/session-001.qmd").read_text()
        assert all(e["sequence"] == i + 1 for i, e in enumerate(events(recording)))
        return normalized(transcript, workspace)


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_export_preserves_images_polled_results_and_sql_state(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary, execution.serve(), current_directory=workspace
        ) as client:
            client.initialize_and_list_tools()
            client.send(sql="CREATE TABLE export_once AS SELECT 7 AS value")
            client.send(sql="INSERT INTO export_once VALUES (11) RETURNING value")
            client.send(
                # fmt: r
                r=code("""
                    counter <- 1L
                    cat("before plot\\n")
                    plot(1:3)
                    cat("after plot\\n")
                    stop("recorded error")
                    """),
            )
            images = [
                block["data"]
                for block in client.transcript[-1]["result"]["content"]
                if block["type"] == "image"
            ]
            assert len(images) == 1
            client.send(
                r='answer <- readline("export gate: "); cat("later result:", answer, "\\n")',
                timeout_ms=60000,
            )
            assert "[waiting for stdin]" in last_result_text(client)
            client.send(stdin="released\n", timeout_ms=60000)
            assert "later result: released" in last_result_text(client)
            client.send(
                r='paths <- .console$export_session("report"); stopifnot(counter == 1L)'
            )
            qmd = (workspace / "report/session.qmd").read_text()
            markdown = (workspace / "report/session.md").read_text()
            assert "INSERT INTO export_once" in qmd
            assert "export_session" not in qmd
            assert "recorded error" in markdown and "later result: released" in markdown
            assert "## Cell 004" in markdown
            assert "Tool call" not in markdown and "Input:" not in markdown
            (image,) = (workspace / "report/images").iterdir()
            assert image.read_bytes() == base64.b64decode(images[0])
            assert f"images/{image.name}" in markdown
            client.send(
                r='stopifnot(identical(DBI::dbGetQuery(.console$sql_connection(), "SELECT value FROM export_once ORDER BY value")$value, c(7L, 11L)))'
            )
            assert not client.transcript[-1]["result"].get("isError")
            copy = workspace / "portable"
            shutil.copytree(workspace / "report", copy)
            assert (copy / "session.md").read_text() == markdown
            assert (copy / "images" / image.name).read_bytes() == image.read_bytes()
            return normalized(client.finish(), workspace)


@requires(POSIX)
@executions(SANDBOXED)
def test_export_obeys_worker_write_permissions(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory).resolve()
        allowed = workspace / "allowed"
        allowed.mkdir()
        filesystem = json.dumps({"read_only": ["/"], "read_write": [str(allowed)]})
        with McpClient(
            binary,
            execution.serve("-c", "sandbox.filesystem=" + filesystem),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(python="print('permission probe')")
            client.send(python="_console.export_session('denied')")
            assert client.transcript[-1]["result"].get("isError")
            assert "cannot create export destination" in last_result_text(client)
            assert not (workspace / "denied").exists()
            client.send(python="paths = _console.export_session('allowed/report')")
            assert not client.transcript[-1]["result"].get("isError")
            assert (allowed / "report/session.md").is_file()
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            client.send(
                python=f"_console.export_session({str(recording / 'working')!r})"
            )
            assert "outside the managed recording" in last_result_text(client)
            return normalized(client.finish(), workspace)


@executions(DIRECT, SANDBOXED)
def test_export_reports_unavailable_capture(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        console = workspace / ".agents/console"
        console.mkdir(parents=True)
        (console / "sessions").write_text("occupied")
        with McpClient(
            binary, execution.serve(), current_directory=workspace
        ) as client:
            client.initialize_and_list_tools()
            client.send(python="counter = 1")
            client.send(python="_console.export_session('report')")
            assert client.transcript[-1]["result"].get("isError")
            assert "session capture unavailable" in last_result_text(client)
            assert not (workspace / "report").exists()
            client.send(python="assert counter == 1")
            assert not client.transcript[-1]["result"].get("isError")
            transcript, stderr = client.finish_with_standard_error()
            assert "transcript recording disabled" in stderr
            assert (console / "sessions").read_text() == "occupied"
            return normalized(transcript, workspace)


if __name__ == "__main__":
    run_this_suite(__file__)
