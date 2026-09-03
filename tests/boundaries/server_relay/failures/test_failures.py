#!/usr/bin/env -S uv run --script

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.server_relay._harness import ServerRelayClient
from support.records import Transcript
from support.suites import run_this_suite


PLATFORMS = {"darwin"}


def _reports_worker_outcome(
    binary: Path,
    scenario: str,
    diagnostic: str,
) -> tuple[Transcript, str]:
    client = ServerRelayClient(binary, scenario)
    failed = client.client._start_send(r="42")
    transcript = client.release_failure(failed, diagnostic)
    result = failed["result"]
    assert result.get("isError") is True, result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    output = content[0]["text"]
    stopped = "[worker stopped: in-memory state lost]"
    replacement = "[starting new worker]"
    assert (
        output.index(diagnostic) < output.index(stopped) < output.index(replacement)
    ), output
    return transcript, output


def test_reports_fatal_failure(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "fatal")
    failed = client.client._start_send(r="42")
    transcript = client.release_failure(failed, "scripted relay failure")
    output = failed["result"]["content"][0]["text"]
    assert output.startswith("drained after fatal failure\n"), output
    assert "[worker exited with status 86]" in output, output
    assert transcript[-1] == {"relay": {"kind": "worker_exited", "code": 86}}, (
        transcript
    )
    return transcript


def test_rejects_truncated_output(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "truncated")
    failed = client.client._start_send(r="42")
    transcript = client.release_failure(
        failed,
        "relay stream closed midway through a frame",
    )
    assert len(transcript) == 1, transcript
    raw = base64.b64decode(transcript[0]["relay_raw"], validate=True)
    assert raw == b'{"kind":"console_output"', raw
    return transcript


def test_reports_unexpected_worker_exit_zero(binary: Path) -> Transcript:
    transcript, _ = _reports_worker_outcome(
        binary,
        "exit_zero",
        "[worker exited with status 0]",
    )
    assert transcript[-1] == {"relay": {"kind": "worker_exited", "code": 0}}, transcript
    return transcript


def test_reports_unexpected_worker_exit_nonzero_and_drains_output(
    binary: Path,
) -> Transcript:
    transcript, output = _reports_worker_outcome(
        binary,
        "exit_nonzero",
        "[worker exited with status 33]",
    )
    assert output.startswith("drained stdout\n�drained stderr\n"), output
    events = [entry["relay"] for entry in transcript if entry.keys() == {"relay"}]
    assert events[-6:] == [
        {"kind": "stdout", "data": "drained stdout\n"},
        {
            "kind": "stderr_bytes",
            "data": base64.b64encode(b"\xffdrained stderr\n").decode("ascii"),
        },
        {"kind": "stdout_closed"},
        {"kind": "stderr_closed"},
        {"kind": "worker_sideband_closed"},
        {"kind": "worker_exited", "code": 33},
    ], events
    return transcript


def test_reports_unexpected_worker_signal(binary: Path) -> Transcript:
    transcript, _ = _reports_worker_outcome(
        binary,
        "signaled",
        "[worker terminated by signal 15]",
    )
    assert transcript[-1] == {"relay": {"kind": "worker_signaled", "signal": 15}}, (
        transcript
    )
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
