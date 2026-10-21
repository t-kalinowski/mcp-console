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
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.assertions import last_result_text
from support.requirements import POSIX, R, SQL, requires
from support.records import Transcript
from support.snapshots import platform_snapshots
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
    for path in sorted(
        {workspace, workspace.resolve()}, key=lambda p: len(str(p)), reverse=True
    ):
        data = data.replace(json.dumps(str(path))[1:-1], "<workspace>")
    return json.loads(data)


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_exports_accepted_live_duckdb_additions(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary,
            execution.serve(
                *(("--writable-root", str(workspace)) if execution == SANDBOXED else ())
            ),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(python="session_identity = object()")
            before = client.send(requirements={"action": "get"})["structuredContent"][
                "requirements"
            ]
            assert "fts" not in before["duckdb"], before
            client.expect("[prepared]", requirements={"duckdb": ["fts"]})
            accepted = client.send(requirements={"action": "get"})["structuredContent"][
                "requirements"
            ]
            assert "fts" in accepted["duckdb"], accepted
            client.send(
                python="assert session_identity is not None; print('same worker')"
            )
            client.send(python="exported = _console.export_session('report')")
            qmd = (workspace / "report/session.qmd").read_text()
            assert "    - fts\n" in qmd, qmd
            assert "eval: false" not in qmd, qmd
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            managed = list((recording / "sessions").glob("*.qmd"))
            assert len(managed) == 1, managed
            assert "    - fts\n" in managed[0].read_text()
            return normalized(client.finish(), workspace)


@platform_snapshots(
    "win32",
    reason="Windows preserves native destination-collision diagnostics and error codes",
)
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
            binary,
            execution.serve(
                *(("--writable-root", str(workspace)) if execution == SANDBOXED else ())
            ),
            env,
            current_directory=workspace,
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
            assert "ir render session.qmd" in qmd, qmd
            assert "#| error: true\nraise ValueError('retained failure')" in qmd, qmd
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
            transcript = client.finish()
        assert (
            len([e for e in events(recording) if e["event"] == "worker_started"]) == 2
        )
        for path in recording.rglob("*"):
            if path.is_file():
                assert not path.stat().st_mode & 0o222, path
        for path in report.rglob("*"):
            if path.is_file():
                assert path.stat().st_mode & 0o200, path
        return normalized(transcript, workspace)


@executions(DIRECT, SANDBOXED)
def test_export_after_automatic_replacement(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary,
            execution.serve(
                *(("--writable-root", str(workspace)) if execution == SANDBOXED else ())
            ),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(python="print('original session')")
            client.send(python="import os; os._exit(17)")
            client.send(python="print('automatic replacement')")
            assert last_result_text(client) == "automatic replacement\n", (
                client.transcript[-1]
            )
            client.send(python="paths = _console.export_session('automatic')")
            automatic = (workspace / "automatic/session.md").read_text()
            assert (
                "automatic replacement" in automatic
                and "original session" not in automatic
            )
            assert "export_session" not in automatic
            transcript = client.finish()
        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        assert (
            len([e for e in events(recording) if e["event"] == "worker_started"]) == 2
        )
        return normalized(transcript, workspace)


@executions(DIRECT, SANDBOXED)
def test_controlled_cells_belong_to_their_selected_session(
    binary: Path, execution: Execution
) -> Transcript:
    return controlled_cells_recording(binary, execution, "python")


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_controlled_r_cells_belong_to_their_selected_session(
    binary: Path, execution: Execution
) -> Transcript:
    return controlled_cells_recording(binary, execution, "r")


def controlled_cells_recording(
    binary: Path, execution: Execution, language: str
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        if language == "python" and os.name != "nt":
            (workspace / "python3").symlink_to(sys.executable)
        cells = [
            (None, "original worker source"),
            ("restart", "replacement worker source"),
            ("interrupt", "surviving worker source"),
        ]
        sources = [f"print({label!r})" for _, label in cells]
        with McpClient(
            binary,
            execution.serve(
                *(("--writable-root", str(workspace)) if execution == SANDBOXED else ())
            ),
            selected_environment(workspace)
            if language == "python"
            else dict(os.environ),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            for (control, label), source in zip(cells, sources, strict=True):
                result = client.send(
                    **{language: source}, **({"control": control} if control else {})
                )
                assert not result.get("isError"), result
                assert label in last_result_text(client), result
            client.send(
                **{
                    language: "paths = _console.export_session('report')"
                    if language == "python"
                    else "paths <- .console$export_session('report')"
                }
            )
            assert not client.transcript[-1]["result"].get("isError"), (
                client.transcript[-1]
            )
            transcript = client.finish()
        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        documents = sorted((recording / "sessions").glob("*.qmd"))
        assert len(documents) == 2, documents
        original, replacement = [path.read_text() for path in documents]
        assert sources[0] in original, original
        assert all(source not in original for source in sources[1:]), original
        assert sources[0] not in replacement, replacement
        assert all(replacement.count(source) == 1 for source in sources[1:]), (
            replacement
        )
        exported = (workspace / "report/session.qmd").read_text()
        assert sources[0] not in exported, exported
        assert all(exported.count(source) == 1 for source in sources[1:]), exported
        capture = events(recording)
        calls = {
            event["request"]["arguments"].get(language): event["call_id"]
            for event in capture
            if event["event"] == "tool_call"
        }
        associations = [
            event["session_id"]
            for event in capture
            if event["event"] == "source_associated"
            and event["call_id"] == calls[sources[1]]
        ]
        assert associations == [2], associations
        return normalized(transcript, workspace)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_exports_recorded_image_formats(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary,
            execution.serve("--worker", str(worker)),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            result = client.send(r="emit image formats")
            images = result["content"]
            assert [image["mimeType"] for image in images] == [
                "image/png",
                "image/jpeg",
            ], result
            assert base64.b64decode(images[1]["data"]).startswith(b"\xff\xd8\xff")
            assert base64.b64decode(images[1]["data"]).endswith(b"\xff\xd9")
            client.send(r="export session")
            bundle = json.loads(last_result_text(client))
            assert [image["name"] for image in bundle["images"]] == [
                "image-000001.png",
                "image-000002.jpg",
            ], bundle["images"]
            for observed, exported in zip(images, bundle["images"], strict=True):
                assert base64.b64decode(exported["data"]) == base64.b64decode(
                    observed["data"]
                )
                assert f"images/{exported['name']}" in bundle["markdown"]
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            (jpeg,) = [
                event
                for event in events(recording)
                if event["event"] == "image_retained"
                and event["mime_type"] == "image/jpeg"
            ]
            assert Path(jpeg["path"]).suffix == ".bin", jpeg
            assert (recording / jpeg["path"]).read_bytes() == base64.b64decode(
                images[1]["data"]
            )
            # Assert bytes and links before shortening their transcript display.
            for observed, exported in zip(images, bundle["images"], strict=True):
                observed["data"] = exported["data"] = (
                    f"<byte-identical {observed['mimeType']} image>"
                )
            client.transcript[-1]["result"]["content"][0]["text"] = (
                json.dumps(bundle, indent=2) + "\n"
            )
            return normalized(client.finish(), workspace)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_replacement_exports_retained_r_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        source = (
            "retained <- packageVersion('praise'); cat('retained praise available\\n')"
        )
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "r.packages=[]",
                *(
                    ("--writable-root", str(workspace))
                    if execution == SANDBOXED
                    else ()
                ),
            ),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "initial praise available\n",
                r="initial <- packageVersion('praise'); cat('initial praise available\\n')",
                requirements={"r": ["praise"]},
            )
            rejected = client.send(
                r="stop('rejected source')",
                requirements={"r": ["uncommittedpackage"], "python": [""]},
            )
            assert rejected["isError"], rejected
            client.send(control="restart")
            client.expect("retained praise available\n", r=source)
            selected = client.send(requirements={"action": "get"})["structuredContent"]
            assert selected["requirements"]["r"] == ["praise"], selected
            client.expect(r='paths <- .console$export_session("report")')
            transcript = client.finish()
        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        capture = events(recording)
        (call,) = [
            event
            for event in capture
            if event["event"] == "tool_call"
            and event["request"].get("arguments", {}).get("r") == source
        ]
        (cell,) = [
            event
            for event in capture
            if event["event"] == "cell_started" and event["call_id"] == call["call_id"]
        ]
        session = cell["owner"]["session_id"]
        for path in (
            recording / f"sessions/session-{session:03}.qmd",
            workspace / "report/session.qmd",
        ):
            qmd = path.read_text()
            assert "  packages:\n    - praise\n" in qmd, qmd
            assert "uncommittedpackage" not in qmd, qmd
            assert source in qmd, qmd
        return normalized(transcript, workspace)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_r_export_without_python_initialization(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary,
            execution.serve(
                *(("--writable-root", str(workspace)) if execution == SANDBOXED else ())
            ),
            current_directory=workspace,
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


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_export_preserves_images_polled_results_and_sql_state(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary,
            execution.serve(
                *(("--writable-root", str(workspace)) if execution == SANDBOXED else ())
            ),
            current_directory=workspace,
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
                block
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
            assert image.read_bytes() == base64.b64decode(images[0]["data"])
            assert f"images/{image.name}" in markdown
            client.send(
                r='stopifnot(identical(DBI::dbGetQuery(.console$sql_connection(), "SELECT value FROM export_once ORDER BY value")$value, c(7L, 11L)))'
            )
            assert not client.transcript[-1]["result"].get("isError")
            copy = workspace / "portable"
            shutil.copytree(workspace / "report", copy)
            assert (copy / "session.md").read_text() == markdown
            assert (copy / "images" / image.name).read_bytes() == image.read_bytes()
            # Native R devices encode PNGs differently across platforms. Keep
            # the byte comparisons above, then normalize only their display.
            images[0]["data"] = "<PNG byte-identical to exported and copied image>"
            return normalized(client.finish(), workspace)


@requires(POSIX)
@platform_snapshots(
    "linux",
    reason="Linux read-only mounts report EROFS; Seatbelt reports EPERM for denied export writes",
)
@executions(SANDBOXED)
def test_export_obeys_worker_write_permissions(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory).resolve()
        allowed = workspace / "allowed"
        allowed.mkdir()
        with McpClient(
            binary,
            execution.serve("--writable-root", str(allowed)),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(python="print('permission probe')")
            client.send(python="_console.export_session('denied')")
            assert "RuntimeError:" in last_result_text(client), client.transcript[-1]
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


@platform_snapshots(
    "win32",
    reason="Windows preserves its native recording-directory creation error",
)
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
            assert "RuntimeError: session capture unavailable:" in last_result_text(
                client
            ), client.transcript[-1]
            assert "session capture unavailable" in last_result_text(client)
            assert not (workspace / "report").exists()
            client.send(python="assert counter == 1")
            assert not client.transcript[-1]["result"].get("isError")
            transcript, stderr = client.finish_with_standard_error()
            assert "transcript recording disabled" in stderr
            assert (console / "sessions").read_text() == "occupied"
            return normalized(transcript, workspace)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_restart_requirements_leave_retired_source_unchanged(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "r.packages=[]",
                *(
                    ("--writable-root", str(workspace))
                    if execution == SANDBOXED
                    else ()
                ),
            ),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("original session\n", r='cat("original session\\n")')
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            original = recording / "sessions/session-001.qmd"
            before = original.read_text()
            client.send(
                control="restart", requirements={"action": "set", "r": ["praise"]}
            )
            client.expect(
                "replacement session\n",
                r='stopifnot(requireNamespace("praise", quietly = TRUE)); cat("replacement session\\n")',
            )
            client.expect(r='paths <- .console$export_session("report")')
            assert original.read_text() == before, original.read_text()
            for path in (
                recording / "sessions/session-002.qmd",
                workspace / "report/session.qmd",
            ):
                qmd = path.read_text()
                assert "  packages:\n    - praise\n" in qmd, qmd
                assert "original session" not in qmd and "replacement session" in qmd
                assert "Requirements selected" not in qmd and "eval: false" not in qmd
            return normalized(client.finish(), workspace)


if __name__ == "__main__":
    run_this_suite(__file__)
