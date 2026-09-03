#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import Transcript, run_this_suite
from server_relay._harness import (
    ServerRelayClient,
    _tool_text,
)


PLATFORMS = {"darwin"}


def test_starts_and_reports_ready(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "ready")
    assert _tool_text(client.send(control="restart")) == (
        "[starting new worker]\n[idle]"
    )
    return client.finish_active()


def test_evaluates_and_commits_operation_result(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "evaluate")
    assert _tool_text(client.send(r="42")) == "[done]"
    transcript = client.finish_active()
    assert not any(
        entry.keys() == {"server"}
        and entry["server"].get("kind") == "terminal_committed"
        for entry in transcript
    ), transcript
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
