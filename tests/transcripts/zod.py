#!/usr/bin/env -S uv run --script

import base64
import json
import os
import signal
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from _support import McpClient, Transcript, code, run_this_suite


PLATFORMS = {"darwin"}
LARGE_OUTPUT_SIZE = 2 * 1024 * 1024
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)


def test_routes_send_over_sideband(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client.initialize_and_list_tools()
    client.call_tool("send", r="echo")
    client.call_tool("send", python="echo")
    client.call_tool("send", sql="echo")
    return client.finish()


def test_returns_worker_images(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client.initialize_and_list_tools()
    client.call_tool("send", r="emit image")
    result = client.transcript[-1]["result"]
    assert result == {
        "content": [
            {"type": "text", "text": "before image\n"},
            {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
            {"type": "text", "text": "after image\n"},
        ],
        "isError": False,
    }, result
    return client.finish()


def test_records_tool_calls_and_images(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            current_directory=workspace,
            umask=0,
        )
        client.initialize_and_list_tools()
        client.call_tool("send", r="emit image")
        client.request(
            "tools/call",
            name="send",
            arguments={"r": "1", "python": "1"},
            _meta={"progressToken": "record-me"},
        )
        client.call_tool(
            "session",
            action="prepare",
            requirements={"python": ["transcript-fixture"]},
        )
        session_result = client.transcript[-1]["result"]
        client.call_tool("missing")
        missing_error = client.transcript[-1]["error"]

        sessions = list((workspace / ".mcp-console" / "sessions").iterdir())
        assert len(sessions) == 1, sessions
        session = sessions[0]
        journal_text = (session / "internal" / "events.jsonl").read_text(
            encoding="utf-8"
        )
        assert PNG_1X1 not in journal_text, journal_text
        events = [json.loads(line) for line in journal_text.splitlines()]
        assert [event["event"] for event in events] == [
            "session_started",
            "tool_call",
            "artifact_created",
            "tool_result",
            "tool_call",
            "tool_result",
            "tool_call",
            "tool_result",
            "tool_call",
            "tool_result",
        ], events
        run_id = events[0]["run_id"]
        assert run_id
        assert session.name == run_id, (session, run_id)
        assert events[0]["session"] == "default", events[0]
        assert Path(events[0]["working_directory"]).samefile(workspace), events[0]
        assert all(event["run_id"] == run_id for event in events), events
        assert all(event["schema_version"] == 1 for event in events), events
        assert [event["sequence"] for event in events] == list(range(1, 11)), events
        assert events[1]["call_id"] == events[2]["call_id"] == 1, events
        assert events[1]["request_id"] == 3, events[1]
        assert events[1]["request"] == {
            "name": "send",
            "arguments": {"r": "emit image"},
        }, events[1]
        assert {
            key: events[2][key]
            for key in ("artifact_id", "call_id", "path", "mime_type", "bytes")
        } == {
            "artifact_id": 1,
            "call_id": 1,
            "path": "artifacts/call-000001-image-000001.png",
            "mime_type": "image/png",
            "bytes": len(base64.b64decode(PNG_1X1)),
        }, events[2]
        assert events[3]["result"] == {
            "content": [
                {"type": "text", "text": "before image\n"},
                {
                    "type": "image",
                    "artifactId": 1,
                    "path": "artifacts/call-000001-image-000001.png",
                    "mimeType": "image/png",
                },
                {"type": "text", "text": "after image\n"},
            ],
            "isError": False,
        }, events[3]
        assert events[4]["call_id"] == events[5]["call_id"] == 2, events
        assert events[4]["request_id"] == 4, events[4]
        assert events[4]["request"] == {
            "name": "send",
            "arguments": {"r": "1", "python": "1"},
            "_meta": {"progressToken": "record-me"},
        }, events[4]
        assert events[5]["result"] == {
            "content": [
                {
                    "type": "text",
                    "text": "only one of `r`, `python`, or `sql` may be supplied",
                }
            ],
            "isError": True,
        }, events[5]
        assert events[6]["call_id"] == events[7]["call_id"] == 3, events
        assert events[6]["request_id"] == 5, events[6]
        assert events[6]["request"] == {
            "name": "session",
            "arguments": {
                "action": "prepare",
                "requirements": {"python": ["transcript-fixture"]},
            },
        }, events[6]
        assert events[7]["result"] == session_result, events[7]
        assert events[8]["call_id"] == events[9]["call_id"] == 4, events
        assert events[8]["request_id"] == 6, events[8]
        assert events[8]["request"] == {
            "name": "missing",
            "arguments": {},
        }, events[8]
        assert events[9]["error"] == missing_error, events[9]

        image_path = session / events[3]["result"]["content"][1]["path"]
        image_bytes = image_path.read_bytes()
        assert image_bytes == base64.b64decode(PNG_1X1), image_path
        directory_modes = {
            path.relative_to(workspace).as_posix(): path.stat().st_mode & 0o777
            for path in (
                workspace / ".mcp-console",
                workspace / ".mcp-console" / "sessions",
                session,
                session / "artifacts",
                session / "internal",
            )
        }
        assert set(directory_modes.values()) == {0o700}, directory_modes
        file_modes = {
            path.relative_to(workspace).as_posix(): path.stat().st_mode & 0o777
            for path in (session / "internal" / "events.jsonl", image_path)
        }
        assert set(file_modes.values()) == {0o600}, file_modes
        transcript = client.finish()

        normalized_journal = journal_text.replace(
            json.dumps(events[0]["working_directory"], ensure_ascii=False),
            json.dumps("<workspace>"),
        ).replace(
            json.dumps(run_id),
            json.dumps("<run ID>"),
        )
        for event in events:
            assert event["at"].endswith("Z"), event
            datetime.fromisoformat(event["at"])
            normalized_journal = normalized_journal.replace(
                json.dumps(event["at"]),
                json.dumps("<UTC timestamp>"),
            )
            event["at"] = "<UTC timestamp>"
            event["run_id"] = "<run ID>"
        events[0]["working_directory"] = "<workspace>"
        assert normalized_journal.endswith("\n"), normalized_journal
        from yaml12 import Yaml

        transcript.append({"journal": events})
        transcript.append(
            {
                "produced session": {
                    "root": ".mcp-console/sessions/<run ID>",
                    "files": {
                        "internal/events.jsonl": normalized_journal,
                        "artifacts/call-000001-image-000001.png": Yaml(
                            base64.b64encode(image_bytes).decode("ascii"),
                            tag="tag:yaml.org,2002:binary",
                        ),
                    },
                }
            }
        )
        return transcript


def test_stops_after_transcript_failure(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            current_directory=workspace,
        )
        client.initialize_and_list_tools()
        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        artifacts = session / "artifacts"
        artifacts.rmdir()
        artifacts.write_text("not a directory", encoding="utf-8")

        client.call_tool("send", r="emit image")
        first_error = client.transcript[-1]["error"]
        assert first_error["code"] == -32603, first_error
        assert "transcript recording failed" in first_error["message"], first_error
        assert "failed to create" in first_error["message"], first_error

        journal = session / "internal" / "events.jsonl"
        journal_after_failure = journal.read_text(encoding="utf-8")
        events = [json.loads(line) for line in journal_after_failure.splitlines()]
        assert [event["event"] for event in events] == [
            "session_started",
            "tool_call",
        ], events
        assert journal_after_failure.endswith("\n"), journal_after_failure

        client.call_tool("send", r="echo")
        second_error = client.transcript[-1]["error"]
        assert second_error["code"] == -32603, second_error
        assert "transcript is unavailable" in second_error["message"], second_error
        assert journal.read_text(encoding="utf-8") == journal_after_failure

        first_error["message"] = "<artifact persistence failed>"
        second_error["message"] = "<transcript unavailable after recording failure>"
        transcript = client.finish()
        transcript.append(
            {
                "journal after failure": [event["event"] for event in events],
                "complete final line": True,
                "post-failure append": False,
            }
        )
        return transcript


def test_flushes_calls_and_keeps_unpolled_images(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        workspace = temporary / "workspace"
        workspace.mkdir()
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
            current_directory=workspace,
        )
        client.initialize_and_list_tools()

        waiting_id = client.next_request_id
        client.next_request_id += 1
        waiting = client.send(
            {
                "jsonrpc": "2.0",
                "id": waiting_id,
                "method": "tools/call",
                "params": {
                    "name": "send",
                    "arguments": {
                        "r": "complete after release",
                        "timeout_ms": 3_000,
                    },
                },
            }
        )
        started = wait_for_marker(
            temporary,
            "zod-evaluation-started",
            client,
        )
        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        journal = session / "internal" / "events.jsonl"
        before_release = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        assert [event["event"] for event in before_release] == [
            "session_started",
            "tool_call",
        ], before_release

        (started.parent / "zod-release-evaluation").touch()
        client.receive(waiting)
        after_release = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        assert [event["event"] for event in after_release] == [
            "session_started",
            "tool_call",
            "tool_result",
        ], after_release

        client.call_tool(
            "send",
            r="emit image before completion",
            timeout_ms=0,
        )
        assert client.transcript[-1]["result"] == {
            "content": [{"type": "text", "text": "\n[running]"}],
            "isError": False,
        }, client.transcript[-1]
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "<leading newline>[running]"
        )
        image_started = wait_for_marker(
            temporary,
            "zod-image-evaluation-started",
            client,
        )
        (image_started.parent / "zod-release-image").touch()
        wait_for_marker(temporary, "zod-image-processed", client)

        final_events = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        assert [event["event"] for event in final_events] == [
            "session_started",
            "tool_call",
            "tool_result",
            "tool_call",
            "tool_result",
            "artifact_created",
        ], final_events
        artifact = final_events[-1]
        assert {
            key: artifact[key]
            for key in ("artifact_id", "call_id", "path", "mime_type", "bytes")
        } == {
            "artifact_id": 1,
            "call_id": 2,
            "path": "artifacts/call-000002-image-000001.png",
            "mime_type": "image/png",
            "bytes": len(base64.b64decode(PNG_1X1)),
        }, artifact
        image_path = session / artifact["path"]
        assert image_path.read_bytes() == base64.b64decode(PNG_1X1), image_path

        (image_started.parent / "zod-release-image-completion").touch()
        client.call_tool("send", timeout_ms=3_000)
        poll_result = client.transcript[-1]["result"]
        assert poll_result == {
            "content": [{"type": "image", "data": PNG_1X1, "mimeType": "image/png"}],
            "isError": False,
        }, poll_result
        polled_events = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        assert [event["event"] for event in polled_events[-2:]] == [
            "tool_call",
            "tool_result",
        ], polled_events
        assert polled_events[-1]["call_id"] == 3, polled_events[-1]
        assert polled_events[-1]["result"] == {
            "content": [
                {
                    "type": "image",
                    "mimeType": "image/png",
                    "artifactId": artifact["artifact_id"],
                    "path": artifact["path"],
                }
            ],
            "isError": False,
        }, polled_events[-1]

        transcript = client.finish()
        transcript.append(
            {
                "live journal": {
                    "while first call was running": [
                        event["event"] for event in before_release
                    ],
                    "after first call completed": [
                        event["event"] for event in after_release
                    ],
                    "unpolled image": {
                        "event": artifact["event"],
                        "path": artifact["path"],
                        "data": "<byte-identical decoded PNG>",
                    },
                    "later poll result": polled_events[-1]["result"],
                }
            }
        )
        return transcript


def test_custom_worker_skips_managed_python_preflight(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment["R_HOME"] = "/mcp-console-custom-worker-must-not-run-rscript"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
        environment,
    )
    client.initialize_and_list_tools()
    # fmt: python
    python = code(r"""
        echo
        """).removesuffix("\n")
    client.call_tool("send", python=python)
    return client.finish()


def test_captures_worker_stdout(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client.initialize_and_list_tools()
    client.call_tool("send", r="emit stdout")
    output = last_tool_text(client)
    assert_large_output(output, "zod stdout 👩🏽‍💻\n")
    client.transcript[-1]["result"]["content"][0]["text"] = (
        "zod stdout 👩🏽‍💻\n<large output>\n"
    )
    return client.finish()


def test_drains_background_stderr_while_idle(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        client.call_tool("send", r="start background stderr")
        assert last_tool_text(client) == "[done]"
        started = wait_for_marker(
            temporary_path,
            "zod-background-stderr-started",
            client,
        )
        (started.parent / "zod-release-background-stderr").touch()
        wait_for_marker(
            temporary_path,
            "zod-background-stderr-emitted",
            client,
        )

        client.call_tool("send", timeout_ms=0)
        output = last_tool_text(client)
        assert output.endswith("\n[idle]"), output[-100:]
        assert_large_output(
            output.removesuffix("\n[idle]"),
            "zod background stderr\n",
        )
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "zod background stderr\n<large output>\n[idle]"
        )
        return client.finish()


def test_times_out_and_polls_running_evaluation(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client.initialize_and_list_tools()
    client.call_tool("send", r="echo")
    client.call_tool(
        "send",
        r="complete after timeout",
        timeout_ms=10,
    )
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "\n[running]", output
    client.call_tool("send", timeout_ms=3_000)
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "zod: complete after timeout\n", output
    client.call_tool("send", r="echo")
    return client.finish()


def test_accepts_idle_stdin(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client.initialize_and_list_tools()

    client.call_tool("send", stdin="cold\n")
    assert last_tool_text(client) == "\n[idle]"
    client.call_tool("send", r="input without request")
    assert last_tool_text(client) == "zod stdin: cold\n"

    client.call_tool("send", stdin="idle\n")
    assert last_tool_text(client) == "\n[idle]"
    client.call_tool("send", r="input without request")
    assert last_tool_text(client) == "zod stdin: idle\n"
    return client.finish()


def test_routes_combined_and_followup_stdin(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client.initialize_and_list_tools()

    client.call_tool(
        "send",
        r="input length without request",
        stdin=("x" * 1024) + "café\0\n",
    )
    client.transcript[-1]["send"]["stdin"] = "<long UTF-8 stdin containing NUL>"
    assert last_tool_text(client) == "zod stdin length: 1030\n"

    client.call_tool("send", r="input without request", timeout_ms=0)
    assert last_tool_text(client) == "\n[running]"
    client.call_tool("send", stdin="followup\n", timeout_ms=3_000)
    assert last_tool_text(client) == "zod stdin: followup\n"

    client.call_tool("send", r="request input")
    assert last_tool_text(client) == '[input requested: "zod> "]\n[stdin needed]'
    client.call_tool("send", stdin="")
    assert last_tool_text(client) == "\n[stdin needed]"
    client.call_tool("send", stdin="prompted\n")
    assert last_tool_text(client) == "zod stdin: prompted\n"

    client.call_tool(
        "send",
        r="input without request then request input",
        stdin="first\n",
        timeout_ms=1_000,
    )
    assert last_tool_text(client) == '[input requested: "second> "]\n[stdin needed]'
    client.call_tool("send", stdin="second\n")
    assert last_tool_text(client) == "zod stdin: first|second\n"

    client.call_tool("send", r="echo", stdin="stale\n")
    assert last_tool_text(client) == "zod: echo\n"
    client.call_tool("send", r="input without request")
    assert last_tool_text(client) == "zod stdin: stale\n"

    client.call_tool("send", r="echo", stdin="x" * (128 * 1024), timeout_ms=1_000)
    client.transcript[-1]["send"]["stdin"] = "<large unread stdin>"
    assert last_tool_text(client) == "zod: echo\n"
    return client.finish()


def test_preserves_unexposed_input_output(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()

        client.call_tool(
            "send",
            r="request input after timeout",
            stdin="answer\n",
            timeout_ms=0,
        )
        assert last_tool_text(client) == "\n[running]"
        waiting = wait_for_marker(
            temporary_path,
            "zod-waiting-to-request-input",
            client,
        )
        (waiting.parent / "zod-release-input-request").touch()
        wait_for_marker(temporary_path, "zod-input-received", client)

        client.call_tool("send", timeout_ms=3_000)
        assert last_tool_text(client) == (
            'before\n[input requested: "late> "]\nduring request\nzod stdin: answer\n'
        )
        return client.finish()


def last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    return result["content"][0]["text"]


def assert_large_output(output: str, prefix: str) -> None:
    expected = prefix + ("x" * LARGE_OUTPUT_SIZE)
    assert output.startswith(expected), (
        f"captured {len(output)} bytes without the complete {len(expected)}-byte payload"
    )
    barrier = output.removeprefix(expected)
    assert barrier and not barrier.strip("y"), "unexpected text after captured payload"


def test_restarts_after_unexpected_sideband_message(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client.initialize_and_list_tools()
            failed_call = client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "send",
                        "arguments": {"r": "violate protocol"},
                    },
                }
            )
            group_marker = wait_for_marker(
                temporary_path,
                "zod-process-group",
                client,
            )
            worker_group = read_worker_group(group_marker)
            client.receive(failed_call)
            result = failed_call["result"]
            assert result["isError"] is True
            assert result["content"][0]["text"] == (
                "zod output before protocol failure\n"
                "[worker sent an unexpected ready message]"
            )
            assert not process_group_exists(worker_group), "Zod outlived its failure"

            restarted_call = client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "send",
                        "arguments": {"r": "complete silently"},
                    },
                }
            )
            client.receive(restarted_call)
            assert last_tool_text(client) == (
                "\n[worker restarted: in-memory state lost]\n"
            )
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_restarts_after_worker_exit(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client.initialize_and_list_tools()
    client.call_tool("send", r="exit unexpectedly")
    assert client.transcript[-1]["result"]["isError"] is True
    client.call_tool("send", stdin="replacement\n")
    assert last_tool_text(client) == (
        "\n[worker restarted: in-memory state lost]\n[idle]"
    )
    client.call_tool("send", r="input without request")
    assert last_tool_text(client) == "zod stdin: replacement\n"
    return client.finish()


def test_explicit_restart_preserves_pending_restart_notice(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client.initialize_and_list_tools()
    client.call_tool("send", r="exit unexpectedly")
    assert client.transcript[-1]["result"]["isError"] is True

    client.call_tool("session", action="restart")
    assert last_tool_text(client) == "[restarted]"

    client.call_tool("send", r="echo")
    assert last_tool_text(client) == (
        "zod: echo\n[worker restarted: in-memory state lost]\n"
    )
    client.call_tool("send", r="echo")
    assert last_tool_text(client) == "zod: echo\n"
    return client.finish()


def test_restart_closes_worker_stdin(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        client.call_tool("send", r="wait for stdin close", timeout_ms=0)
        assert last_tool_text(client) == "\n[running]"
        wait_for_marker(
            temporary_path,
            "zod-waiting-for-stdin-close",
            client,
        )

        client.call_tool("session", action="restart")
        assert last_tool_text(client) == "[restarted]"

        client.call_tool("send", r="echo")
        output = last_tool_text(client)
        prefix = "zod stdin closed\n" + ("x" * LARGE_OUTPUT_SIZE)
        assert output.startswith(prefix), "worker stdin did not close before restart"
        assert output.endswith("zod: echo\n")
        barrier = output.removeprefix(prefix).removesuffix("zod: echo\n")
        assert barrier and not barrier.strip("y"), "unexpected old-worker output"
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "zod stdin closed\n<large output>\nzod: echo\n"
        )
        return client.finish()


def test_restart_force_stops_stalled_worker(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client.initialize_and_list_tools()
            client.call_tool("send", r="stall", timeout_ms=0)
            assert last_tool_text(client) == "\n[running]"
            group_marker = wait_for_marker(
                temporary_path,
                "zod-process-group",
                client,
            )
            worker_group = read_worker_group(group_marker)
            wait_for_marker(temporary_path, "zod-stalled", client)

            restart_call = client.send(
                {
                    "jsonrpc": "2.0",
                    "id": client.next_request_id,
                    "method": "tools/call",
                    "params": {
                        "name": "session",
                        "arguments": {"action": "restart"},
                    },
                }
            )
            client.next_request_id += 1
            wait_for_process_group_exit(worker_group, client)
            client.receive(restart_call)
            assert last_tool_text(client) == "[restarted]"

            client.call_tool("send", r="echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_restart_starts_first_worker_and_waits_until_ready(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_release = temporary_path / "zod-startup-release"
        startup_control.write_text("block", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["ZOD_STARTUP_RELEASE"] = str(startup_release)
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client.initialize_and_list_tools()
            restarted = client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "session",
                        "arguments": {"action": "restart"},
                    },
                }
            )
            wait_for_marker(
                temporary_path,
                "zod-replacement-waiting-ready",
                client,
            )
            worker_group = read_worker_group(
                wait_for_marker(temporary_path, "zod-process-group", client)
            )

            while_restarting = client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "send",
                        "arguments": {"r": "echo"},
                    },
                }
            )
            client.receive(while_restarting)
            result = while_restarting["result"]
            assert result["isError"] is True
            assert result["content"][0]["text"] == "worker is restarting"

            startup_release.touch()
            client.receive(restarted)
            assert restarted["result"]["content"][0]["text"] == "[restarted]"

            after_restart = client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "send",
                        "arguments": {"r": "echo"},
                    },
                }
            )
            client.receive(after_restart)
            assert after_restart["result"]["content"][0]["text"] == "zod: echo\n"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_restart_discards_unread_stdin(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client.initialize_and_list_tools()
    client.call_tool("send", stdin="stale\n")
    assert last_tool_text(client) == "\n[idle]"

    client.call_tool("session", action="restart")
    assert last_tool_text(client) == "[restarted]"

    client.call_tool("send", r="input without request", stdin="fresh\n")
    assert last_tool_text(client) == "zod stdin: fresh\n"
    return client.finish()


def test_reports_restart_notice_on_next_response(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_release = temporary_path / "zod-startup-release"
        startup_control.write_text("ready", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["ZOD_STARTUP_RELEASE"] = str(startup_release)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        client.call_tool("send", r="exit unexpectedly")
        assert client.transcript[-1]["result"]["isError"] is True

        startup_control.write_text("block", encoding="utf-8")
        client.call_tool("send", r="complete after release", timeout_ms=0)
        assert last_tool_text(client) == "\n[running]"
        wait_for_marker(temporary_path, "zod-replacement-waiting-ready", client)
        startup_release.touch()
        evaluation_started = wait_for_marker(
            temporary_path,
            "zod-evaluation-started",
            client,
        )

        client.call_tool("send", r="echo")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "[worker is already evaluating a cell; poll without a code field]"
            "\n[worker restarted: in-memory state lost]\n"
        )

        (evaluation_started.parent / "zod-release-evaluation").touch()
        client.call_tool("send", timeout_ms=3_000)
        output = last_tool_text(client)
        assert output == "zod: complete after release\n", repr(output)
        client.call_tool("send", r="echo")
        assert last_tool_text(client) == "zod: echo\n"
        return client.finish()


def test_retries_initial_startup_silently(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        startup_control = Path(temporary_directory) / "zod-startup-control"
        startup_control.write_text("fail", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        client.call_tool("send", r="echo")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "worker sideband read failed: worker sideband closed"
        )
        startup_control.write_text("ready", encoding="utf-8")
        client.call_tool("send", r="echo")
        assert last_tool_text(client) == "zod: echo\n"
        return client.finish()


def test_runs_worker_inside_sandbox(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        host_file = Path(temporary_directory) / "host.txt"
        host_file.write_text("host data", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_SANDBOX_PROBE_PATH"] = str(host_file)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        client.call_tool("send", r="probe sandbox")
        transcript = client.finish()

        assert host_file.read_text(encoding="utf-8") == "host data"
        return transcript


def test_shuts_down_stalled_worker(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        environment = os.environ.copy()
        temporary_path = Path(temporary_directory)
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client.initialize_and_list_tools()
            stalled = client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "send",
                        "arguments": {
                            "r": "stall",
                            "stdin": "x" * (2 * 1024 * 1024),
                        },
                    },
                }
            )
            stalled["send"]["stdin"] = "<large stdin>"
            group_marker = wait_for_marker(temporary_path, "zod-process-group", client)
            worker_group = read_worker_group(group_marker)
            wait_for_marker(temporary_path, "zod-stalled", client)
            client.stdin.close()
            try:
                return_code = client.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    "mcp-console did not stop its stalled worker"
                ) from None

            assert return_code == 0, client.stderr.read()
            client.stdout.read()
            assert client.stderr.read() == ""
            assert not process_group_exists(worker_group), "Zod outlived mcp-console"
            passed = True
            return client.transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_shutdown_deadline_does_not_wait_for_sideband_writer(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_BLOCK_SIDEBAND_WRITE"] = "1"
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client.initialize_and_list_tools()
            entry = client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "send",
                        "arguments": {"r": "x" * (2 * 1024 * 1024)},
                    },
                }
            )
            group_marker = wait_for_marker(
                temporary_path,
                "zod-process-group",
                client,
            )
            worker_group = read_worker_group(group_marker)
            wait_for_marker(
                temporary_path,
                "zod-sideband-blocked",
                client,
            )
            entry["send"]["r"] = "<large cell>"
            shutdown_started = time.monotonic()
            client.stdin.close()
            try:
                return_code = client.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    "mcp-console did not enforce its worker shutdown deadline"
                ) from None
            shutdown_elapsed = time.monotonic() - shutdown_started

            assert shutdown_elapsed < 1.5, (
                f"worker shutdown took {shutdown_elapsed:.3f} seconds"
            )
            assert return_code == 0, client.stderr.read()
            client.stdout.read()
            assert client.stderr.read() == ""
            assert not process_group_exists(worker_group), "Zod outlived mcp-console"
            passed = True
            return client.transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def wait_for_marker(root: Path, name: str, client: McpClient) -> Path:
    deadline = time.monotonic() + 3
    while True:
        markers = list(root.glob(f"**/{name}"))
        if markers:
            assert len(markers) == 1, f"found multiple {name} markers"
            return markers[0]
        assert client.process.poll() is None, "mcp-console stopped before Zod stalled"
        assert time.monotonic() < deadline, "Zod did not report its stall checkpoint"
        time.sleep(0.01)


def read_worker_group(marker: Path) -> int:
    worker_group = int(marker.read_text(encoding="utf-8"))
    assert worker_group != os.getpgrp(), "Zod did not enter a dedicated process group"
    return worker_group


def process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_process_group_exit(process_group: int, client: McpClient) -> None:
    deadline = time.monotonic() + 3
    while process_group_exists(process_group):
        assert client.process.poll() is None, "mcp-console stopped during restart"
        assert time.monotonic() < deadline, (
            "restart did not enforce its shutdown deadline"
        )
        time.sleep(0.01)


def stop_process_group(process_group: int | None) -> None:
    if process_group is None:
        return
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


if __name__ == "__main__":
    run_this_suite(__file__)
