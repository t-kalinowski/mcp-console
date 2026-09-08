#!/usr/bin/env -S uv run --script

import json
import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.checkpoints import (
    FifoCheckpoint,
    release_fixture_checkpoint,
    wait_for_worker_file,
)
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite

PENDING_TEXT_BUDGET = 8 * 1024 * 1024


@executions(DIRECT, SANDBOXED)
def test_separates_startup_omissions_from_retained_cell_text(
    binary: Path, execution: Execution
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with (
        tempfile.TemporaryDirectory() as temporary,
        McpClient(
            binary,
            execution.serve(
                "--worker",
                str(fixtures / "zod"),
                "--relay",
                str(fixtures / "server_relay" / "scripted_relay.py"),
            ),
            {
                **os.environ,
                "TMPDIR": temporary,
                "MCP_CONSOLE_TEST_RELAY_SCENARIO": "startup_output",
            },
        ) as client,
    ):
        client.initialize_and_list_tools()
        client.send(r="42")
        output = last_tool_text(client)
        assert client.temporary_directory is not None
        workspace = Path(client.temporary_directory.name)
        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        path = f".mcp-console/sessions/{session.name}/outputs/call-000001.log"
        assert (workspace / path).read_bytes() == b"cell output\n"
        notices = (
            "\n[output truncated: omitted 7 text bytes and 0 encoded image bytes across 1 event]"
            "\n[output truncated: omitted 12 text bytes and 0 encoded image bytes across 1 event; "
            f"retained text: {path} (12 of 12 omitted text bytes)]"
        )
        prefix = "s" * PENDING_TEXT_BUDGET
        assert output == prefix + notices, output[-1000:]
        client.transcript[-1]["result"]["content"][0]["text"] = (
            f"<retained {PENDING_TEXT_BUDGET} startup text bytes>"
            + notices.replace(session.name, "<run ID>")
        )
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_reports_partial_retention_and_later_unretained_output(
    binary: Path, execution: Execution
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    file_limit = 3 * PENDING_TEXT_BUDGET + 3
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        launcher = workspace / "limited-server"
        # Limit regular-file writes using the OS, while leaving enough room for
        # the journal and both projections of the two bounded tool responses.
        # fmt: python
        launcher.write_text(
            f"#!{sys.executable}\n"
            + code(f"""
            import os
            import resource
            import signal
            import sys

            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE, ({file_limit}, {file_limit}))
            os.execv({str(binary)!r}, [{str(binary)!r}, *sys.argv[1:]])
            """),
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        with McpClient(
            launcher,
            execution.serve("--worker", str(fixtures / "zod")),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(r="overflow cell output file", timeout_ms=120_000)
            first = client.transcript[-1]
            first_text = last_tool_text(client)
            client.send(stdin="continue\n", timeout_ms=120_000)
            second = client.transcript[-1]
            second_text = last_tool_text(client)
            assert "retained text:" not in second_text, second_text[-1000:]

            session = next((workspace / ".mcp-console" / "sessions").iterdir())
            path = f".mcp-console/sessions/{session.name}/outputs/call-000001.log"
            assert (workspace / path).read_bytes() == b"x" * file_limit
            omitted = 2 * PENDING_TEXT_BUDGET + 7
            persisted = file_limit - PENDING_TEXT_BUDGET
            assert (
                f"retained text: {path} ({persisted} of {omitted} omitted text bytes)"
                in first_text
            ), first_text[-1000:]
            assert f"stopped after {file_limit} retained bytes" in first_text, (
                first_text[-1000:]
            )
            assert "later text is not retained in this file" in first_text, first_text[
                -1000:
            ]

            for entry, text, character in (
                (first, first_text, "x"),
                (second, second_text, "y"),
            ):
                prefix = character * PENDING_TEXT_BUDGET
                assert text.startswith(prefix), text[-1000:]
                entry["result"]["content"][0]["text"] = (
                    f"<retained {PENDING_TEXT_BUDGET} text bytes>"
                    + text.removeprefix(prefix).replace(session.name, "<run ID>")
                )

            client.send(r="echo after failure")
            assert last_tool_text(client) == "zod: after failure\n"
            assert (
                session / "outputs/call-000003.log"
            ).read_bytes() == b"zod: after failure\n"
            events = [
                json.loads(line)
                for line in (session / "internal/events.jsonl").read_text().splitlines()
            ]
            output_event = next(
                event for event in events if event["event"] == "cell_output"
            )
            assert output_event["retained_bytes"] == file_limit, output_event
            assert output_event["discarded_bytes"] == PENDING_TEXT_BUDGET + 11, (
                output_event
            )
            assert output_event["inline_omitted_bytes"] == omitted + 7, output_event
            markdown = (session / "transcript.md").read_text()
            assert (
                f"{PENDING_TEXT_BUDGET + 11} bytes not retained in this file"
                in markdown
            )
            assert "permanently discarded" not in markdown
            transcript, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr
            return transcript


@executions(DIRECT, SANDBOXED)
def test_reports_omitted_bytes_retained_at_the_file_limit(
    binary: Path, execution: Execution
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with (
        tempfile.TemporaryDirectory() as temporary,
        McpClient(
            binary,
            execution.serve("--worker", str(fixtures / "zod")),
            {**os.environ, "TMPDIR": temporary},
        ) as client,
    ):
        client.initialize_and_list_tools()
        client.send(r="complete silently")
        assert last_tool_text(client) == "[done]"
        client.send(r="overflow cell retention limit", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        release = wait_for_worker_file(
            Path(temporary), "zod-release-retention-output", client
        )
        with closing(
            FifoCheckpoint.attach(release.with_name("zod-retention-completed"))
        ) as completed:
            release_fixture_checkpoint(release)
            # Keep all output pending until the server acknowledges completion;
            # intermediate polls would reset the inline budget and split counts.
            completed.wait(timeout=client.response_timeout)
        client.send(timeout_ms=0)
        output = last_tool_text(client)
        assert client.temporary_directory is not None
        workspace = Path(client.temporary_directory.name)
        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        path = f".mcp-console/sessions/{session.name}/outputs/call-000002.log"
        limit = 1024 * 1024 * 1024
        assert (workspace / path).stat().st_size == limit, (
            (workspace / path).stat().st_size,
            output[-1500:],
        )
        with (workspace / path).open("rb") as retained:
            block = b"x" * (1024 * 1024)
            for _ in range(1024):
                assert retained.read(len(block)) == block
            assert retained.read(1) == b""
        omitted_retained = limit - PENDING_TEXT_BUDGET
        notices = (
            f"\n[output truncated: omitted {omitted_retained + 5} text bytes and "
            "0 encoded image bytes across 128 events; "
            f"retained text: {path} ({omitted_retained} of {omitted_retained + 5} omitted text bytes)]"
            f"\n[cell output retention limit reached at {limit} bytes for {path}; "
            "later text is not retained in this file]\n"
        )
        prefix = "x" * PENDING_TEXT_BUDGET
        assert output == prefix + notices, output[-1500:]
        events = [
            json.loads(line)
            for line in (session / "internal/events.jsonl").read_text().splitlines()
        ]
        summary = next(
            event
            for event in events
            if event["event"] == "cell_output" and event["call_id"] == 2
        )
        assert summary["retained_bytes"] == limit, summary
        assert summary["discarded_bytes"] == 5, summary
        assert summary["inline_omitted_bytes"] == omitted_retained + 5, summary
        client.transcript[-1]["result"]["content"][0]["text"] = (
            f"<retained {PENDING_TEXT_BUDGET} text bytes>"
            + notices.replace(session.name, "<run ID>")
        )
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
