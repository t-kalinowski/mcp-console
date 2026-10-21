"""Export path failures remain ordinary runtime errors."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.recording.test_export import events, normalized
from support.assertions import last_result_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.r import r_test_environment
from support.resolvers import bare_runtime_environment
from support.requirements import NON_UTF8_FILENAMES, R, requires
from support.suites import run_this_suite
from support.snapshots import platform_snapshots


@platform_snapshots(
    "win32",
    reason="Windows resolves parent traversal during path lookup; Unix rejects its missing component",
)
@executions(DIRECT, SANDBOXED)
def test_rejects_traversal_into_recording(
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
            client.send(python="token = object()")
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            destination = (
                "missing/../" + recording.relative_to(workspace).as_posix() + "/export"
            )
            client.send(python=f"_console.export_session({destination!r})")
            assert "RuntimeError:" in last_result_text(client), client.transcript[-1]
            expected = (
                "outside the managed recording"
                if os.name == "nt"
                else "invalid export destination"
            )
            assert expected in last_result_text(client), client.transcript[-1]
            assert not (recording / "export").exists()
            assert not (workspace / "missing").exists()
            client.send(python="assert token is not None")
            assert not client.transcript[-1]["result"].get("isError")
            return normalized(client.finish(), workspace)


@requires(NON_UTF8_FILENAMES, R)
@executions(DIRECT)
def test_non_utf8_capture_returns_runtime_error(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory) / os.fsdecode(b"workspace-\xff")
        workspace.mkdir()
        environment, _ = r_test_environment()
        library = Path(directory) / "library"
        library.mkdir()
        environment = bare_runtime_environment(environment, library)
        with McpClient(
            binary, execution.serve("-c", "cache=host"), environment, workspace
        ) as client:
            client.initialize_and_list_tools()
            client.send(r="token <- new.env(); writeLines('original worker')")
            assert last_result_text(client) == "original worker\n", client.transcript[
                -1
            ]
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            client.send(r='paths <- .console$export_session("report")')
            assert "Error" in last_result_text(client), client.transcript[-1]
            assert "UTF-8" in last_result_text(client), client.transcript[-1]
            assert not (workspace / "report").exists()
            client.send(r="stopifnot(is.environment(token)); writeLines('same worker')")
            assert last_result_text(client) == "same worker\n", client.transcript[-1]
            transcript, stderr = client.finish_with_standard_error()
            assert (
                len([e for e in events(recording) if e["event"] == "worker_started"])
                == 1
            )
            assert "transcript projections disabled" in stderr, stderr
            return normalized(transcript, workspace)


@requires(NON_UTF8_FILENAMES, R)
@executions(DIRECT, SANDBOXED)
def test_non_utf8_destination_is_rejected_before_writing(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        destination = workspace / os.fsdecode(b"destination-\xff")
        with McpClient(
            binary,
            execution.serve(
                *(("--writable-root", str(workspace)) if execution == SANDBOXED else ())
            ),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("ready\n", r='token <- new.env(); cat("ready\\n")')
            client.expect(
                # fmt: python
                python=code(r"""
                    import os

                    os.mkdir(b"destination-\xff")
                    os.chdir(b"destination-\xff")
                    """),
            )
            for arguments in (
                {"r": '.console$export_session("reports/r")'},
                {"python": '_console.export_session("reports/python")'},
            ):
                client.send(**arguments)
                assert "export destination requires a UTF-8 path" in last_result_text(
                    client
                )
                assert not (destination / "reports").exists()
            client.expect(python='os.chdir("..")')
            client.expect(
                r='stopifnot(is.environment(token)); paths <- .console$export_session("report")'
            )
            assert (workspace / "report/session.qmd").is_file()
            assert (workspace / "report/session.md").is_file()
            return normalized(client.finish(), workspace)


if __name__ == "__main__":
    run_this_suite(__file__)
