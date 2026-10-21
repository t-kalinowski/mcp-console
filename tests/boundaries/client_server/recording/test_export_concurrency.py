"""Export reconstruction leaves live MCP capture available."""

import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_without_r import selected_environment
from boundaries.client_server.recording.test_export import events, normalized
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, requires
from support.suites import run_this_suite


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
                    LOADER_VARIABLE: str(
                        build_interposer(workspace, "export_scan_checkpoint")
                    ),
                    "MCP_CONSOLE_TEST_EXPORT_SCAN_ARMED": str(armed),
                    "MCP_CONSOLE_TEST_EXPORT_SCAN_REACHED": str(reached.path),
                    "MCP_CONSOLE_TEST_EXPORT_SCAN_RELEASE": str(release.path),
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


if __name__ == "__main__":
    run_this_suite(__file__)
