#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.processes import stop_process
from support.records import Transcript
from support.suites import run_this_suite

PLATFORMS = {"darwin"}

from boundaries.client_server._harness import (
    submit_prompted_stdin,
    wait_for_marker,
)


def test_accepts_idle_stdin(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(stdin="cold\n")
    assert last_tool_text(client) == "\n[idle]"
    client.send(r="input without request")
    assert last_tool_text(client) == "zod stdin: cold\n"

    client.send(stdin="idle\n")
    assert last_tool_text(client) == "\n[idle]"
    client.send(r="input without request")
    assert last_tool_text(client) == "zod stdin: idle\n"
    return client._finish()


def test_idle_stdin_startup_blocks_preparation(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_release = temporary_path / "zod-startup-release"
        startup_control.write_text("block", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["ZOD_STARTUP_RELEASE"] = str(startup_release)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        passed = False
        try:
            client._initialize_and_list_tools()
            idle_stdin = client._start_send(stdin="queued\n")
            wait_for_marker(
                temporary_path,
                "zod-replacement-waiting-ready",
                client,
            )

            preparation = client._start_send(
                requirements={"python": ["py-yaml12"]},
            )
            client._receive(preparation)
            assert preparation["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": "[requirements not prepared: worker is starting]",
                    }
                ],
                "isError": True,
            }, preparation

            startup_release.touch()
            client._receive(idle_stdin)
            assert idle_stdin["result"] == {
                "content": [{"type": "text", "text": "\n[idle]"}],
                "isError": False,
            }, idle_stdin

            client.send(r="input without request")
            assert last_tool_text(client) == "zod stdin: queued\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            startup_release.touch()
            if not passed:
                stop_process(client.process)


def test_routes_combined_and_followup_stdin(binary: Path) -> Transcript:
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
        client._initialize_and_list_tools()

        client.send(
            r="input length without request",
            stdin=("x" * 1024) + "café\0\n",
        )
        client.transcript[-1]["send"]["stdin"] = "<long UTF-8 stdin containing NUL>"
        assert last_tool_text(client) == "zod stdin length: 1030\n"

        client.send(r="input without request", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        client.send(stdin="followup\n")
        assert last_tool_text(client) == "zod stdin: followup\n"

        client.send(r="request input")
        assert last_tool_text(client) == (
            '[input requested: "zod> "]\n[waiting for stdin]'
        )
        client.send(stdin="")
        assert last_tool_text(client) == "\n[waiting for stdin]"
        submit_prompted_stdin(
            client,
            temporary_path,
            "prompted\n",
            "zod-prompted-input-processed",
            "zod stdin: prompted\n",
        )

        client.send(
            r="input without request then request input",
            stdin="first\n",
        )
        assert last_tool_text(client) == (
            '[input requested: "second> "]\n[waiting for stdin]'
        )
        submit_prompted_stdin(
            client,
            temporary_path,
            "second\n",
            "zod-combined-input-processed",
            "zod stdin: first|second\n",
        )

        client.send(r="echo echo", stdin="stale\n")
        assert last_tool_text(client) == "zod: echo\n"
        client.send(r="input without request")
        assert last_tool_text(client) == "zod stdin: stale\n"

        client.send(r="echo echo", stdin="x" * (128 * 1024))
        client.transcript[-1]["send"]["stdin"] = "<large unread stdin>"
        assert last_tool_text(client) == "zod: echo\n"
        return client._finish()


def test_routes_same_call_stdin_to_direct_fd0(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="read fd 0 directly", stdin="direct café\n")
    assert last_tool_text(client) == "zod fd 0: 'direct café\\n'\n"
    return client._finish()


def test_preserves_unexposed_input_output(binary: Path) -> Transcript:
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
        client._initialize_and_list_tools()

        client.send(
            r="request input after timeout",
            stdin="answer\n",
            timeout_ms=0,
        )
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        waiting = wait_for_marker(
            temporary_path,
            "zod-waiting-to-request-input",
            client,
        )
        (waiting.parent / "zod-release-input-request").touch()
        wait_for_marker(temporary_path, "zod-input-received", client)

        client.send(timeout_ms=3_000)
        assert last_tool_text(client) == (
            'before\n[input requested: "late> "]\nduring request\nzod stdin: answer\n'
        )
        return client._finish()


if __name__ == "__main__":
    run_this_suite(__file__)
