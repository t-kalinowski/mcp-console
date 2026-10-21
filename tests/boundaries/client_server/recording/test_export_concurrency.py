"""Export reconstruction leaves live MCP capture available."""

import base64
import json
import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_without_r import selected_environment
from boundaries.client_server.recording.test_export import events, normalized
from support.assertions import last_result_text, wait_for_evaluation_output
from support.progress import without_elapsed
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, R, requires
from support.suites import run_this_suite


def checkpoint_environment(workspace: Path) -> dict[str, str]:
    return {
        LOADER_VARIABLE: str(build_interposer(workspace, "export_scan_checkpoint")),
        "MCP_CONSOLE_TEST_EXPORT_SCAN_ARMED": str(workspace / "scan-armed"),
        "MCP_CONSOLE_TEST_EXPORT_SCAN_REACHED": str(workspace / "scan-reached"),
        "MCP_CONSOLE_TEST_EXPORT_SCAN_RELEASE": str(workspace / "scan-release"),
    }


@requires(NATIVE_FIXTURES)
@executions(DIRECT)
def test_capture_continues_while_export_scans_the_journal(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        (workspace / "python3").symlink_to(sys.executable)
        armed = workspace / "scan-armed"
        with (
            closing(FifoCheckpoint.create(workspace / "scan-reached")) as reached,
            closing(FifoCheckpoint.create(workspace / "scan-release")) as release,
            McpClient(
                binary,
                execution.serve(),
                {
                    **selected_environment(workspace),
                    **checkpoint_environment(workspace),
                },
                current_directory=workspace,
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect(
                "before export\n", python="counter = 41; print('before export')"
            )
            armed.touch()
            exporting = client.start_send(
                python="paths = _console.export_session('report'); print(counter + 1)"
            )
            timeout = client.response_timeout
            try:
                reached.wait("export journal scan is held", client=client)
                client.response_timeout = 10
                inspection = client.send(requirements={"action": "get"})
                assert not inspection.get("isError"), inspection
            finally:
                client.response_timeout = timeout
                release.release()
            client.receive(exporting)
            assert exporting["result"]["content"] == [{"type": "text", "text": "42\n"}]
            qmd = (workspace / "report/session.qmd").read_text()
            report = (workspace / "report/session.md").read_text()
            assert "counter = 41" in qmd and "before export" in report
            assert "export_session" not in qmd and "export_session" not in report
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            transcript, stderr = client.finish_with_standard_error()
            assert "recording disabled" not in stderr, stderr
            capture = events(recording)
            assert any(
                event["event"] == "tool_result" and event["call_id"] == 3
                for event in capture
            ), capture
            return normalized(transcript, workspace)


@requires(NATIVE_FIXTURES)
@executions(DIRECT)
def test_output_and_restart_continue_while_export_scans(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        (workspace / "python3").symlink_to(sys.executable)
        with (
            closing(FifoCheckpoint.create(workspace / "scan-reached")) as reached,
            closing(FifoCheckpoint.create(workspace / "scan-release")) as release,
            closing(FifoCheckpoint.create(workspace / "produce")) as produce,
            closing(FifoCheckpoint.create(workspace / "produced")) as produced,
            McpClient(
                binary,
                execution.serve(),
                {
                    **selected_environment(workspace),
                    **checkpoint_environment(workspace),
                },
                current_directory=workspace,
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect("ready\n", python="print('ready')")
            (workspace / "scan-armed").touch()
            exporting = client.start_send(
                timeout_ms=0,
                # fmt: python
                python=code("""
                    import os
                    from pathlib import Path
                    from threading import Thread


                    def produce_during_export():
                        with Path("produce").open("rb", buffering=0) as gate:
                            assert gate.read(1) == b"1"
                        os.write(1, b"observed while export is held\\n")
                        with Path("produced").open("wb", buffering=0) as gate:
                            gate.write(b"1")


                    Thread(target=produce_during_export, daemon=True).start()
                    paths = _console.export_session("report")
                    print("retired export returned")
                    """),
            )
            timeout = client.response_timeout
            try:
                reached.wait("export journal scan is held", client=client)
                client.receive(exporting)
                produce.release()
                produced.wait("background output has been emitted", client=client)
                poll = client.send(timeout_ms=3000)
                assert "observed while export is held\n" in last_result_text(client), (
                    poll
                )
                client.response_timeout = 10
                replacement = client.send(
                    control="restart", python="print('replacement responsive')"
                )
                assert not replacement.get("isError"), replacement
                assert "replacement responsive\n" in last_result_text(client), (
                    replacement
                )
                assert not (workspace / "report").exists()
            finally:
                client.response_timeout = timeout
                release.release()
            client.expect(python="paths = _console.export_session('replacement')")
            report = (workspace / "replacement/session.md").read_text()
            assert "replacement responsive" in report
            assert "retired export returned" not in report
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            transcript, stderr = client.finish_with_standard_error()
            assert "recording disabled" not in stderr, stderr
            assert (recording / "capture/sessions/001/cell-002.txt").read_text() == (
                "observed while export is held\n"
            )
            assert (
                len([e for e in events(recording) if e["event"] == "worker_started"])
                == 2
            )
            return normalized(transcript, workspace)


@requires(NATIVE_FIXTURES)
@executions(DIRECT)
def test_shutdown_does_not_wait_for_a_stalled_export_scan(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        (workspace / "python3").symlink_to(sys.executable)
        with (
            closing(FifoCheckpoint.create(workspace / "scan-reached")) as reached,
            closing(FifoCheckpoint.create(workspace / "scan-release")) as release,
            McpClient(
                binary,
                execution.serve(),
                {
                    **selected_environment(workspace),
                    **checkpoint_environment(workspace),
                },
                current_directory=workspace,
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect("ready\n", python="print('ready')")
            (workspace / "scan-armed").touch()
            exporting = client.start_send(
                python="paths = _console.export_session('report')", timeout_ms=0
            )
            try:
                reached.wait("export journal scan is held", client=client)
                client.receive(exporting)
                transcript, stderr = client.finish_with_standard_error()
                assert "recording disabled" not in stderr, stderr
                assert not (workspace / "report").exists()
            finally:
                release.release()
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            assert events(recording)[-1]["event"] == "recording_ended"
            assert not (recording / "capture/events.jsonl").stat().st_mode & 0o222
            return normalized(transcript, workspace)


@requires(NATIVE_FIXTURES, R)
@executions(SANDBOXED)
def test_export_retains_the_validated_image_after_path_replacement(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        secret = workspace / "private"
        secret.write_text("worker-denied synthetic private data\n")
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "sandbox": {
                        "filesystem": {
                            "read_write": [str(workspace), str(config.parent)],
                            "deny": [str(secret)],
                        }
                    }
                }
            )
        )
        with (
            closing(FifoCheckpoint.create(workspace / "scan-reached")) as reached,
            closing(FifoCheckpoint.create(workspace / "scan-release")) as release,
            closing(FifoCheckpoint.create(workspace / "replace")) as replace,
            closing(FifoCheckpoint.create(workspace / "replaced")) as replaced,
            McpClient(
                binary,
                execution.serve(),
                {
                    **os.environ,
                    **checkpoint_environment(workspace),
                    "MCP_CONSOLE_TEST_EXPORT_SCAN_MARKER": "---\n# Source recorded by MCP Console;",
                },
                current_directory=workspace,
            ) as client,
        ):
            client.initialize_and_list_tools()
            result = client.send(r="plot(1:3)")
            (image,) = [
                block for block in result["content"] if block["type"] == "image"
            ]
            original = base64.b64decode(image["data"])
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            (retained,) = [
                e for e in events(recording) if e["event"] == "image_retained"
            ]
            captured = recording / retained["path"]
            assert captured.read_bytes() == original
            (workspace / "scan-armed").touch()
            exporting = client.start_send(
                # fmt: python
                python=code(f"""
                    from pathlib import Path
                    from threading import Thread

                    private = Path({str(secret)!r})
                    try:
                        private.read_bytes()
                    except PermissionError:
                        print("private read denied")
                    else:
                        raise AssertionError("private file was readable")

                    def replace_image():
                        with Path("replace").open("rb", buffering=0) as gate:
                            assert gate.read(1) == b"1"
                        image = Path({str(captured)!r})
                        image.unlink()
                        image.symlink_to(private)
                        with Path("replaced").open("wb", buffering=0) as gate:
                            gate.write(b"1")

                    thread = Thread(target=replace_image, daemon=True)
                    thread.start()
                    paths = _console.export_session("report")
                    thread.join()
                    print("export complete")
                    """),
            )
            try:
                reached.wait(
                    "export has validated images and started transfer", client=client
                )
                replace.release()
                replaced.wait("worker replaced the retained image", client=client)
                assert captured.is_symlink()
                release.release()
                client.receive(exporting)
                progress = "\n[running; poll with an empty send]"
                observed = without_elapsed(last_result_text(client))
                if observed.endswith(progress):
                    wait_for_evaluation_output(
                        client,
                        None,
                        "exported image completion",
                        initial_cuts=(observed.removesuffix(progress),),
                        completion_timeout_seconds=client.response_timeout,
                    )
                assert "export complete\n" in last_result_text(client), (
                    client.transcript[-1]
                )
                exported = list((workspace / "report/images").iterdir())
                assert len(exported) == 1, exporting
                assert exported[0].read_bytes() == original
                image["data"] = "<PNG preserved despite capture path replacement>"
            finally:
                release.release()
                # Restore the fixture before recording finalization touches it.
                if captured.is_symlink():
                    captured.unlink()
                    captured.write_bytes(original)
                    captured.chmod(0o400)
            return normalized(client.finish(), workspace)


if __name__ == "__main__":
    run_this_suite(__file__)
