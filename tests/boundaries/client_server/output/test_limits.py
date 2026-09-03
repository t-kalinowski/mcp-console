#!/usr/bin/env -S uv run --script

import array
import base64
import fcntl
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import termios
import time
from datetime import datetime
from pathlib import Path
from typing import Self

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import (
    FifoCheckpoint,
    McpClient,
    Transcript,
    TranscriptWithCompanions,
    build_manager_interposer,
    code,
    r_test_environment,
    run_this_suite,
    stop_client,
)

PLATFORMS = {"darwin"}
LARGE_OUTPUT_SIZE = 2 * 1024 * 1024
PENDING_TEXT_BUDGET = 8 * 1024 * 1024
TEST_GATED_RESPONSE_SIZE = 128 * 1024
FIXTURE_CHECKPOINT_TIMEOUT_SECONDS = 15
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)
TEST_EVENT_FIFO_NAME = "zod-test-events"
TEST_CONTROL_FIFO_NAME = "zod-test-control"
TEST_CLEANUP_FIFO_NAME = "zod-test-cleanup"
TEST_RESPONSE_QUERY_FIFO_NAME = "zod-test-response-query"
TEST_RESPONSE_RESULT_FIFO_NAME = "zod-test-response-result"
TEST_CONTROL_READY_NAME = "zod-test-control-ready"


from client_server._harness import (
    large_output,
    _zod_last_tool_text as last_tool_text,
)


def test_bounds_pending_output_and_resets_after_completion(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="overflow console output")
    output = last_tool_text(client)
    retained = "x" * PENDING_TEXT_BUDGET
    notice = (
        "\n[output truncated: omitted 7 text bytes and "
        "0 encoded image bytes across 1 event]"
    )
    assert output == retained + notice, (
        f"unexpected bounded output: length={len(output)}, tail={output[-200:]!r}"
    )
    client.transcript[-1]["result"]["content"][0]["text"] = (
        f"<retained {PENDING_TEXT_BUDGET} text bytes>{notice}"
    )

    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"
    return client._finish()


def test_orders_failure_and_replacement_output(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        startup_control = Path(temporary_directory) / "zod-startup-control"
        startup_control.write_text("ready", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()

        client.send(r="complete silently")
        assert last_tool_text(client) == "[done]"
        startup_control.write_text("ready", encoding="utf-8")
        client.send(r="violate protocol after stdout")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert len(result["content"]) == 1, result
        output = result["content"][0]["text"]
        raw = large_output("zod old stdout\n")
        notices = [
            "[worker sent an unexpected ready message]",
            "[worker terminated by signal 9]",
            "[worker stopped: in-memory state lost]",
            "[starting new worker]",
            "[idle]",
        ]
        assert output.count(raw) == 1, "protocol failure lost raw stdout bytes"
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
        return client._finish()


def test_preserves_raw_output_during_forced_stop(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

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
    return client._finish()


if __name__ == "__main__":
    run_this_suite(__file__)
