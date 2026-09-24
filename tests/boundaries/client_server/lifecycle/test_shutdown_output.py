#!/usr/bin/env -S uv run --script

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient, stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.records import Transcript
from support.requirements import PROCESS_EVENTS, requires
from support.suites import run_this_suite

from boundaries.client_server._harness import ZodFixtureControl


def _shutdown_with_collected_output(
    binary: Path, execution: Execution, *, completed: bool
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment = os.environ.copy()
    with ZodFixtureControl() as control:
        control.configure(environment)
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        try:
            client.initialize_and_list_tools()
            client.send(r="shutdown output checkpoints", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            control.connect(client)
            control.wait_for(0, "evaluation_started")
            control.send_control(0, "emit_output")
            control.wait_for(0, "output_processed")

            waiting = client.start_send(stdin="p", timeout_ms=40_000)
            # The fixture consumes this ownership probe; keep the public snapshot unchanged.
            waiting["send"].pop("stdin")
            control.send_control(0, "observe_poll_ownership", request=waiting["id"])
            ownership = control.wait_for(waiting["id"], "poll_ownership_observed")
            assert ownership["target_operation"] == 0, ownership
            client.send(timeout_ms=0)
            assert client.transcript[-1]["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": "[worker evaluation is already being polled]",
                    }
                ],
                "isError": True,
            }, client.transcript[-1]
            if completed:
                control.send_control(0, "complete")
                control.wait_for(0, "completion_processed")

            client.stdin.close()
            return_code = client.process.wait(timeout=3)
            client.receive(waiting)
            result = waiting["result"]
            content = result["content"]
            assert content[0] == {"type": "text", "text": "before shutdown\n"}, result
            assert content[1]["type"] == "image", result
            assert content[2]["type"] == "text", result
            assert content[2]["text"].startswith("after image\n"), result
            assert return_code == 0, client.stderr.read()
            assert client.stdout.read() == ""
            assert client.stderr.read() == ""
            return client.transcript
        finally:
            stop_client(client)


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_eof_preserves_output_from_active_cell(
    binary: Path, execution: Execution
) -> Transcript:
    return _shutdown_with_collected_output(binary, execution, completed=False)


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_eof_preserves_output_after_cell_completion(
    binary: Path, execution: Execution
) -> Transcript:
    return _shutdown_with_collected_output(binary, execution, completed=True)


@executions(DIRECT, SANDBOXED)
def test_eof_preserves_first_send_response(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    try:
        client.initialize_and_list_tools()
        waiting = client.start_send(r="1", python="1")
        client.stdin.close()
        assert client.process.wait(timeout=3) == 0, client.stderr.read()
        client.receive(waiting)
        assert waiting["result"] == {
            "content": [
                {
                    "type": "text",
                    "text": "only one of `r`, `python`, or `sql` may be supplied",
                }
            ],
            "isError": True,
        }, waiting
        assert client.stdout.read() == ""
        assert client.stderr.read() == ""
        return client.transcript
    finally:
        stop_client(client)


if __name__ == "__main__":
    run_this_suite(__file__)
