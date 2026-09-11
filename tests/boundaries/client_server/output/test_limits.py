#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import large_output, last_tool_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import SHARED_LIBRARY_FLAG
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, requires
from support.suites import run_this_suite

PENDING_TEXT_BUDGET = 8 * 1024 * 1024
CELL_OUTPUT_RETENTION_LIMIT = 1024 * 1024 * 1024


@executions(DIRECT, SANDBOXED)
def test_bounds_pending_output_and_resets_after_completion(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            current_directory=workspace,
            umask=0,
        )
        client.initialize_and_list_tools()

        client.send(r="overflow console output")
        overflow = client.transcript[-1]
        output = last_tool_text(client)
        session = next((workspace / ".agents/console" / "sessions").iterdir())
        relative_output = Path("outputs/call-000001.log")
        public_output = (
            f".agents/console/sessions/{session.name}/{relative_output.as_posix()}"
        )
        retained = "x" * PENDING_TEXT_BUDGET
        notice = (
            "\n[output truncated: omitted 7 text bytes and "
            "0 encoded image bytes across 1 event; "
            f"retained text: {public_output} (7 of 7 omitted text bytes)]"
        )
        assert output == retained + notice, (
            f"unexpected bounded output: length={len(output)}, tail={output[-300:]!r}"
        )

        output_path = session / relative_output
        assert output_path.read_text(encoding="utf-8") == "x" * (
            PENDING_TEXT_BUDGET + 7
        )
        assert output_path.stat().st_mode & 0o777 == 0o600, output_path

        events = [
            json.loads(line)
            for line in (session / "internal" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        cell_output = next(event for event in events if event["event"] == "cell_output")
        assert {
            key: cell_output[key]
            for key in (
                "call_id",
                "path",
                "retained_bytes",
                "inline_omitted_bytes",
                "discarded_bytes",
                "retention_limit_bytes",
            )
        } == {
            "call_id": 1,
            "path": relative_output.as_posix(),
            "retained_bytes": PENDING_TEXT_BUDGET + 7,
            "inline_omitted_bytes": 7,
            "discarded_bytes": 0,
            "retention_limit_bytes": CELL_OUTPUT_RETENTION_LIMIT,
        }, cell_output
        markdown = (session / "transcript.md").read_text(encoding="utf-8")
        assert f"[Retained text output for call 1](<{relative_output}>)" in markdown
        assert relative_output.as_posix() not in (session / "transcript.qmd").read_text(
            encoding="utf-8"
        )

        normalized_notice = notice.replace(session.name, "<run ID>")
        overflow["result"]["content"][0]["text"] = (
            f"<retained {PENDING_TEXT_BUDGET} text bytes>{normalized_notice}"
        )

        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"
        assert (session / "outputs" / "call-000002.log").read_text(
            encoding="utf-8"
        ) == "zod: echo\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_orders_failure_and_replacement_output(
    binary: Path, execution: Execution
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as temporary_directory:
        interposer = Path(temporary_directory) / "relay-stdout-read.dylib"
        subprocess.run(
            [
                "cc",
                SHARED_LIBRARY_FLAG,
                "-fPIC",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                interposer,
                fixtures / "native" / "relay_stdout_read_interposer.c",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_RELAY_BINARY"] = str(binary)
        environment["MCP_CONSOLE_TEST_RELAY_READ_DYLIB"] = str(interposer)
        # Hold the final raw read until retirement joins it. The worker waits
        # for that read before sending the protocol violation.
        environment["MCP_CONSOLE_TEST_RELAY_READ_MATCH"] = "zod stdout tail\n"
        with McpClient(
            binary,
            execution.serve(
                "--worker",
                str(fixtures / "zod"),
                "--relay",
                str(fixtures / "retirement_read_relay"),
            ),
            environment,
        ) as client:
            client.initialize_and_list_tools()

            client.send(r="complete silently")
            assert last_tool_text(client) == "[done]"
            client.send(r="violate protocol after stdout")
            result = client.transcript[-1]["result"]
            assert result["isError"] is True, result
            assert len(result["content"]) == 1, result
            output = result["content"][0]["text"]
            raw = large_output("zod old stdout\n") + "zod stdout tail\n"
            notices = [
                "[worker sent an unexpected ready message]",
                "[worker terminated by signal 9]",
                "[worker stopped: in-memory state lost]",
                "[starting new worker]",
                "[idle]",
            ]
            assert output.count(raw) == 1, (
                f"protocol failure lost raw stdout bytes: length={len(output)}, "
                f"tail={output[-500:]!r}"
            )
            assert all(output.count(notice) == 1 for notice in notices), repr(output)
            assert [output.index(notice) for notice in notices] == sorted(
                output.index(notice) for notice in notices
            ), repr(output)
            remainder = output.replace(raw, "")
            for notice in notices:
                remainder = remainder.replace(notice, "")
            assert not remainder.replace("\n", ""), repr(output)
            result["content"][0]["text"] = (
                "zod old stdout\n<large output>\n"
                "<cross-source position follows serialized observation>\n"
                "[worker sent an unexpected ready message]\n"
                "[worker terminated by signal 9]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            )

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_preserves_raw_output_during_forced_stop(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
    )
    client.initialize_and_list_tools()

    for stream in ("stdout", "stderr"):
        client.send(r=f"force stop after raw {stream}")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert len(result["content"]) == 1, result
        output = result["content"][0]["text"]
        raw = f"zod retiring {stream}: �"
        notices = [
            "[worker sent an unexpected ready message]",
            "[worker terminated by signal 9]",
            "[worker stopped: in-memory state lost]",
            "[starting new worker]",
            "[idle]",
        ]
        assert output.count(raw) == 1, repr(output)
        assert all(output.count(notice) == 1 for notice in notices), repr(output)
        assert [output.index(notice) for notice in notices] == sorted(
            output.index(notice) for notice in notices
        ), repr(output)
        remainder = output.replace(raw, "")
        for notice in notices:
            remainder = remainder.replace(notice, "")
        assert not remainder.replace("\n", ""), repr(output)
        result["content"][0]["text"] = (
            f"{raw}\n<cross-source position follows serialized observation>\n"
            + "\n".join(notices)
        )

    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
