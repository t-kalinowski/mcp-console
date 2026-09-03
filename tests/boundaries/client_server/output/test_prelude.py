#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import (
    McpClient,
    Transcript,
    run_this_suite,
)

PLATFORMS = {"darwin"}

from client_server._harness import (
    expose_idle_sideband_output,
    _zod_last_tool_text as last_tool_text,
    release_fixture_checkpoint,
    stop_process,
    submit_prompted_stdin,
    wait_for_marker,
)


def test_demarcates_idle_prelude_across_cell_outcomes(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        passed = False
        try:
            client._initialize_and_list_tools()

            expose_idle_sideband_output(client, temporary_path, "success")
            client.send(r="echo echo")
            assert last_tool_text(client) == (
                "zod background sideband\n[output produced while idle]\nzod: echo\n"
            )

            expose_idle_sideband_output(client, temporary_path, "timeout")
            timed_out = client._start_send(
                r="output then complete after release",
                timeout_ms=0,
            )
            pending = wait_for_marker(
                temporary_path,
                "zod-cell-output-pending",
                client,
            )
            client._receive(timed_out)
            assert timed_out["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "zod background sideband\n\n"
                            "[running; poll with an empty send]"
                        ),
                    }
                ],
                "isError": False,
            }, timed_out

            release_fixture_checkpoint(pending.parent / "zod-release-cell-output")
            processed = wait_for_marker(
                temporary_path,
                "zod-cell-output-processed",
                client,
            )
            client.send(timeout_ms=0)
            assert client.transcript[-1]["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": "zod cell output before completion\n\n"
                        "[running; poll with an empty send]",
                    }
                ],
                "isError": False,
            }, client.transcript[-1]

            (processed.parent / "zod-release-evaluation").touch()
            client.send()
            assert last_tool_text(client) == (
                "zod: output then complete after release\n"
            ), repr(last_tool_text(client))

            expose_idle_sideband_output(client, temporary_path, "input")
            client.send(r="request input")
            assert last_tool_text(client) == (
                "zod background sideband\n"
                "[output produced while idle]\n"
                '[input requested: "zod> "]\n'
                "[waiting for stdin]"
            )
            submit_prompted_stdin(
                client,
                temporary_path,
                "answer\n",
                "zod-prompted-input-processed",
                "zod stdin: answer\n",
            )

            expose_idle_sideband_output(client, temporary_path, "language-error")
            client.send(r="language error")
            assert last_tool_text(client) == (
                "zod background sideband\n"
                "[output produced while idle]\n"
                "zod language error\n"
            )

            expose_idle_sideband_output(client, temporary_path, "replacement")
            client.send(r="exit unexpectedly")
            result = client.transcript[-1]["result"]
            assert result["isError"] is True, result
            assert result["content"] == [
                {
                    "type": "text",
                    "text": (
                        "zod background sideband\n"
                        "[output produced while idle]\n"
                        "[worker sideband read failed: worker sideband closed]\n"
                        "[worker exited with status 86]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                }
            ], result
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process(client.process)


if __name__ == "__main__":
    run_this_suite(__file__)
