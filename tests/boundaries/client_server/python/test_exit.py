"""Distinguish Python language exceptions from actual worker process exits."""

from pathlib import Path

from boundaries.client_server.server.test_no_r import no_r_client
from support.assertions import last_result_text
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.records import Transcript


@executions(DIRECT, SANDBOXED)
def test_system_exit_preserves_live_state(
    binary: Path, execution: Execution
) -> Transcript:
    with no_r_client(binary, execution) as client:
        client.initialize_and_list_tools()
        client.send(python="import sys\nanswer = 42\nsys.exit(33)")
        result = client.transcript[-1]["result"]
        assert result["isError"] is False, result
        assert last_result_text(client).endswith("SystemExit: 33\n")
        client.send(python="answer")
        assert last_result_text(client) == "42\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_reports_process_exit_status(binary: Path, execution: Execution) -> Transcript:
    with no_r_client(binary, execution) as client:
        client.initialize_and_list_tools()
        client.send(python="import os\nanswer = 42\nos._exit(33)")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert last_result_text(client) == (
            "[worker sideband read failed: worker sideband closed]\n"
            "[worker exited with status 33]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )
        client.send(python='"answer" in globals()')
        assert last_result_text(client) == "False\n"
        client.send(python="1 + 1")
        assert last_result_text(client) == "2\n"
        return client.finish()
