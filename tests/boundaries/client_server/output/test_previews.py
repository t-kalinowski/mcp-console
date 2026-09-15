#!/usr/bin/env -S uv run --script

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.previews import (
    OMISSION,
    assert_preview,
    compact_previews,
    normalize_preview_paths,
    session_directory,
)
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.records import Transcript
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_reports_raw_and_rendered_counts_for_invalid_utf8(
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
                str(fixtures / "server_relay/scripted_relay.py"),
            ),
            {
                **os.environ,
                "TMPDIR": temporary,
                "MCP_CONSOLE_TEST_RELAY_SCENARIO": "preview_raw",
            },
        ) as client,
    ):
        client.initialize_and_list_tools()
        client.send(r="42")
        text = last_tool_text(client)
        raw = b"raw head\n" + "€".encode() + b"\xff" * 20000 + "€ raw tail\n".encode()
        assert_preview(text, raw.decode("utf-8", errors="replace"))
        assert f"{len(raw)} raw bytes retained, 0 raw bytes not retained" in text
        assert client.temporary_directory is not None
        session = next(
            (
                Path(client.temporary_directory.name) / ".agents/console/sessions"
            ).iterdir()
        )
        assert (session / "outputs/call-000001.log").read_bytes() == raw
        normalize_preview_paths(client)
        compact_previews(client, "�")
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_keeps_partial_idle_utf8_out_of_cell_omission_counts(
    binary: Path, execution: Execution
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with McpClient(
        binary,
        execution.serve(
            "--worker",
            str(fixtures / "zod"),
            "--relay",
            str(fixtures / "server_relay/scripted_relay.py"),
        ),
        {**os.environ, "MCP_CONSOLE_TEST_RELAY_SCENARIO": "preview_raw_prelude"},
    ) as client:
        client.initialize_and_list_tools()
        client.send(r="42")
        text = last_tool_text(client)
        idle = (b"idle head\n" + b"s" * 20000 + b"\xe2").decode(errors="replace")
        cell = "cell head\n" + "x" * 32768 + "\ncell tail\n"
        markers = list(OMISSION.finditer(text))
        assert len(markers) == 2, text
        head, tail = text[: markers[0].start()], text[markers[1].end() :]
        assert idle.startswith(head) and cell.endswith(tail)
        assert len(head.encode()) + int(markers[0][1]) == len(idle.encode())
        assert int(markers[1][1]) + len(tail.encode()) == len(cell.encode())
        assert "no retained cell log" in markers[0][0]
        assert "outputs/call-000001.log" in markers[1][0]
        session = session_directory(client)
        assert (session / "outputs/call-000001.log").read_text() == cell
        summaries = [
            json.loads(line)
            for line in (session / "internal/events.jsonl").read_text().splitlines()
            if json.loads(line)["event"] == "cell_output"
        ]
        assert summaries[-1]["inline_omitted_bytes"] == int(markers[1][1])
        normalize_preview_paths(client)
        compact_previews(client, "s", "x")
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
