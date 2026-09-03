#!/usr/bin/env -S uv run --script

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.server_relay._harness import (
    CHECKPOINT_NAME,
    DONE_NAME,
    RELEASE_NAME,
    ServerRelayClient,
)
from support.assertions import tool_text as _tool_text
from support.records import Transcript
from support.suites import run_this_suite


PLATFORMS = {"darwin"}


def test_forwards_raw_stdout_and_stderr(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "raw_output")
    assert _tool_text(client.send(r="42")) == (
        "stdout text 👩🏽‍💻\nstderr text\n�stdout bytes\n�stderr bytes\n"
    )
    transcript = client.finish_active()

    output = [
        entry["relay"]
        for entry in transcript
        if entry.keys() == {"relay"}
        and entry["relay"].get("kind")
        in {"stdout", "stderr", "stdout_bytes", "stderr_bytes"}
    ]
    assert output[:2] == [
        {"kind": "stdout", "data": "stdout text 👩🏽‍💻\n"},
        {"kind": "stderr", "data": "stderr text\n"},
    ]
    assert [base64.b64decode(event["data"], validate=True) for event in output[2:]] == [
        b"\xffstdout bytes\n",
        b"\xfestderr bytes\n",
    ]
    return transcript


def test_interleaved_stream_ends_prior_redraw_run(
    binary: Path,
) -> Transcript:
    client = ServerRelayClient(binary, "interleaved_stream_redraws")
    assert _tool_text(client.send(r="42")) == ("stderr oldstdout final\nstderr final\n")
    return client.finish_active()


def test_malformed_byte_completes_pending_redraw(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "raw_malformed_redraw")
    assert _tool_text(client.send(r="42")) == "�\n"
    return client.finish_active()


def test_empty_raw_close_does_not_split_console_redraw(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "empty_raw_close_between_redraws")
    assert _tool_text(client.send(r="42")) == "new\n"
    return client.finish_active()


def test_forwards_stdin(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "stdin")
    assert _tool_text(client.send(r="42", stdin="answer\n")) == "[done]"
    return client.finish_active()


def test_empty_stdin_sends_no_relay_command(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "evaluate")
    assert _tool_text(client.send(r="42", stdin="")) == "[done]"
    return client.finish_active()


def test_orders_cross_source_output_by_serialized_observation(
    binary: Path,
) -> Transcript:
    client = ServerRelayClient(binary, "serialized_cross_source_order")
    evaluation = client.client._start_send(r="42")
    checkpoint = client._wait_for(CHECKPOINT_NAME)
    try:
        client.client._receive(evaluation)
        assert _tool_text(evaluation["result"]) == (
            "stdout before completion\n"
            "stderr before completion\n"
            "stdout after completion\n"
            "stderr after completion\n"
            "idle callback after completion\n"
        )
    finally:
        checkpoint.with_name(RELEASE_NAME).touch()
    client._wait_for(DONE_NAME)
    idle_output = _tool_text(client.send())
    assert idle_output == "stdout after grace\n\n[idle]", repr(idle_output)
    transcript = client.finish_active()

    completed = next(
        index
        for index, entry in enumerate(transcript)
        if entry == {"relay": {"kind": "completed"}}
    )
    first_callback = next(
        index
        for index, entry in enumerate(transcript)
        if entry.keys() == {"relay"} and entry["relay"].get("kind") == "resolve_r"
    )
    assert all(
        entry.keys() == {"relay"} for entry in transcript[completed:first_callback]
    )
    r_failures = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"}
        and entry["server"].get("kind") == "r_resolution_failed"
    ]
    assert r_failures == [
        {
            "kind": "r_resolution_failed",
            "failure": "host",
            "message": (
                "automatic R package name `github::owner/repo` is not accepted: "
                "names must start with an ASCII letter, end with an ASCII letter "
                "or digit, and contain only ASCII letters, digits, and dots"
            ),
        }
    ]
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
