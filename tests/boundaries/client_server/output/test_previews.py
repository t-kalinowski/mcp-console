#!/usr/bin/env -S uv run --script

import json
import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.allocations import AllocationProfile
from support.assertions import last_tool_text
from support.previews import (
    CONTROL_OMISSION,
    OMISSION,
    assert_preview,
    compact_previews,
    normalize_preview_paths,
    session_directory,
)
from support.client import McpClient
from support.checkpoints import (
    FifoCheckpoint,
    release_fixture_checkpoint,
    wait_for_worker_file,
)
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, requires
from support.suites import run_this_suite

TEXT_BUDGET = 8 * 1024


def check_preview(binary: Path, execution: Execution, scenario: str) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with (
        tempfile.TemporaryDirectory() as temporary,
        McpClient(
            binary,
            execution.serve("--worker", str(worker)),
            current_directory=Path(temporary),
        ) as client,
    ):
        client.initialize_and_list_tools()
        client.send(r=scenario)
        result = client.transcript[-1]["result"]
        text = "".join(
            block["text"] for block in result["content"] if block["type"] == "text"
        )
        assert len(text.encode("utf-8")) <= TEXT_BUDGET, len(text.encode("utf-8"))
        assert text.startswith("preview head\n"), text[:300]
        assert "preview tail: final diagnostic\n" in text, text[-300:]
        assert [block["type"] for block in result["content"]] == [
            "text",
            "image",
            "text",
        ]
        assert result["content"][-1]["text"] == "after final image\n"
        assert not result.get("isError", False), result
        session = next((Path(temporary) / ".agents/console/sessions").iterdir())
        raw = (session / "outputs/call-000001.log").read_bytes()
        assert raw.startswith(b"preview head\n")
        assert raw.endswith(b"\npreview tail: final diagnostic\nafter final image\n")
        if scenario == "preview redraw":
            assert (
                text
                == "preview head\nprogress final\npreview tail: final diagnostic\nafter final image\n"
            )
        else:
            assert "omitted" in text
            assert "rendered UTF-8 bytes" in text
            assert (
                f".agents/console/sessions/{session.name}/outputs/call-000001.log"
                in text
            )
            assert "Console server" in text
        for block in result["content"]:
            if block["type"] == "text":
                block["text"] = block["text"].replace(session.name, "<run ID>")
        client.send()
        assert last_tool_text(client) == "\n[idle]"
        client.send(r="echo fresh")
        assert last_tool_text(client) == "zod: fresh\n"
        compact_previews(client, "x", "y", "z", "s", "p", "ab", "�")
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_keeps_actual_tail_and_image_after_huge_line(
    binary: Path, execution: Execution
) -> Transcript:
    return check_preview(binary, execution, "preview huge line")


@executions(DIRECT, SANDBOXED)
def test_keeps_actual_tail_after_tiny_event_flood(
    binary: Path, execution: Execution
) -> Transcript:
    return check_preview(binary, execution, "preview tiny events")


@requires(NATIVE_FIXTURES)
def test_bounds_metadata_for_alternating_tiny_events(binary: Path) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with (
        tempfile.TemporaryDirectory() as temporary,
        closing(AllocationProfile(Path(temporary))) as profile,
        McpClient(
            binary,
            DIRECT.serve("--worker", str(worker)),
            {**os.environ, **profile.environment},
        ) as client,
    ):
        client.initialize_and_list_tools()
        client.send(r="echo ready")
        profile.start()
        client.send(r="preview alternating bytes")
        text = last_tool_text(client)
        assert client.request("ping")["result"] == {}
        _, largest = profile.stop()
        assert text == "ab" * 4096
        # This fits the text budget. Retaining one part per event would allocate
        # a large vector and repeatedly scan it before any text is omitted.
        assert largest <= 128 * 1024, largest
        compact_previews(client, "ab")
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_compacts_redraws_before_preview_limits(
    binary: Path, execution: Execution
) -> Transcript:
    return check_preview(binary, execution, "preview redraw")


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
        compact_previews(client, "x", "y", "z", "s", "p", "ab", "�")
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_keeps_tail_when_recording_is_disabled(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        (workspace / ".agents").mkdir()
        (workspace / ".agents/console").write_text("occupied")
        with McpClient(
            binary,
            execution.serve("--worker", str(worker)),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(r="preview huge line")
            text = "".join(
                block["text"]
                for block in client.transcript[-1]["result"]["content"]
                if block["type"] == "text"
            )
            assert len(text.encode()) <= TEXT_BUDGET
            assert text.startswith("preview head\n")
            assert text.endswith("preview tail: final diagnostic\nafter final image\n")
            assert (
                "no retained cell log" in text and "omitted text is unavailable" in text
            )
            assert "outputs/call-" not in text
            compact_previews(client, "x", "y", "z", "s", "p", "ab", "�")
            transcript, stderr = client.finish_with_standard_error()
            assert stderr.count("transcript recording disabled:") == 1, stderr
            return transcript


@executions(DIRECT, SANDBOXED)
def test_preserves_active_prompt_and_state_after_text_flood(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with McpClient(binary, execution.serve("--worker", str(worker))) as client:
        client.initialize_and_list_tools()
        client.send(r="preview prompt")
        text = last_tool_text(client)
        assert len(text.encode()) <= TEXT_BUDGET
        assert text.startswith("prompt output head\n")
        assert "prompt output tail\n" in text
        assert 'input requested: "prompt head ' in text and ' prompt tail> "]' in text
        assert text.endswith("[waiting for stdin]")
        prompt = text[text.index("[input requested:") :].removesuffix(
            "[waiting for stdin]"
        )
        assert_preview(
            prompt,
            '[input requested: "prompt head ' + "p" * 20000 + ' prompt tail> "]\n',
            pattern=CONTROL_OMISSION,
        )
        normalize_preview_paths(client)
        client.send(stdin="answer\n")
        assert last_tool_text(client) == "received answer\n"
        compact_previews(client, "x", "y", "z", "s", "p", "ab", "�")
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_image_overflow_preserves_later_text_and_fitting_image(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with McpClient(binary, execution.serve("--worker", str(worker))) as client:
        client.initialize_and_list_tools()
        client.send(r="preview image limit")
        content = client.transcript[-1]["result"]["content"]
        assert [block["type"] for block in content] == ["text", "image", "text"]
        assert content[0]["text"] == (
            "before oversized image\n\n"
            "[image limit: omitted 1 images (8388612 encoded bytes); 0 already recorded, 1 not retained]\n"
            "before accepted image\n"
        )
        assert content[-1]["text"] == "after accepted image\n"
        assert client.temporary_directory is not None
        session = next(
            (
                Path(client.temporary_directory.name) / ".agents/console/sessions"
            ).iterdir()
        )
        assert len(list((session / "artifacts").iterdir())) == 1
        compact_previews(client, "x", "y", "z", "s", "p", "ab", "�")
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_combines_old_worker_and_replacement_cell_under_one_budget(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with (
        tempfile.TemporaryDirectory() as temporary,
        McpClient(
            binary,
            execution.serve("--worker", str(worker)),
            {**os.environ, "TMPDIR": temporary},
        ) as client,
    ):
        client.initialize_and_list_tools()
        client.send(r="overflow cell output file", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        release = wait_for_worker_file(
            Path(temporary), "zod-release-spooled-output", client
        )
        with closing(
            FifoCheckpoint.attach(release.with_name("zod-spooled-output-processed"))
        ) as processed:
            release_fixture_checkpoint(release)
            processed.wait(
                "old worker output observed", timeout=client.response_timeout
            )
        client.send(control="restart", r="preview huge line")
        result = client.transcript[-1]["result"]
        text = "".join(
            block["text"] for block in result["content"] if block["type"] == "text"
        )
        assert len(text.encode()) <= TEXT_BUDGET
        assert text.startswith("x")
        assert (
            "[active evaluation stopped by session restart request]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]"
        ) in text
        assert "preview tail: final diagnostic\n" in text
        assert text.endswith("after final image\n[done]")
        assert "outputs/call-000001.log" in text and "outputs/call-000002.log" in text
        assert not result.get("isError", False), result
        normalize_preview_paths(client)
        client.send()
        assert last_tool_text(client) == "\n[idle]"
        compact_previews(client, "x", "y", "z", "s", "p", "ab", "�")
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_keeps_partial_idle_utf8_out_of_cell_omission_counts(
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
                "MCP_CONSOLE_TEST_RELAY_SCENARIO": "preview_raw_prelude",
            },
        ) as client,
    ):
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
        compact_previews(client, "x", "y", "z", "s", "p", "ab", "�")
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
