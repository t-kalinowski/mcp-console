#!/usr/bin/env -S uv run --script

import base64
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import (
    McpClient,
    Transcript,
    TranscriptWithCompanions,
    r_test_environment,
    run_this_suite,
    stop_client,
)

PLATFORMS = {"darwin"}
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)

from client_server._harness import (
    record_resolved_r_library,
    wait_for_marker,
)


def test_materializes_records_only_for_console_use(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        unused_workspace = temporary / "unused"
        unused_workspace.mkdir()
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            {**os.environ, "TMPDIR": str(unused_workspace)},
            current_directory=unused_workspace,
        )
        client._initialize_and_list_tools()
        assert not (unused_workspace / ".mcp-console").exists(), unused_workspace
        removed = client._request(
            "tools/call",
            name="session",
            arguments={"action": "restart"},
        )
        assert removed["error"] == {
            "code": -32602,
            "message": "tool not found",
        }, removed
        assert not (unused_workspace / ".mcp-console").exists(), unused_workspace
        assert not list(unused_workspace.glob("mcp-console-tmp-*")), unused_workspace
        transcript = client._finish()
        assert not (unused_workspace / ".mcp-console").exists(), unused_workspace

        workspace = temporary / "send"
        workspace.mkdir()
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        assert not (workspace / ".mcp-console").exists(), workspace
        client.send(r="echo echo")

        sessions = list((workspace / ".mcp-console" / "sessions").iterdir())
        assert len(sessions) == 1, sessions
        events = [
            json.loads(line)
            for line in (sessions[0] / "internal" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert [event["event"] for event in events] == [
            "session_started",
            "tool_call",
            "tool_result",
        ], events
        assert events[1]["request"]["name"] == "send", events[1]
        client._finish()

        transcript.append(
            {
                "recording": {
                    "initialization and removed session tool only": "absent",
                    "materialized by": {"send": [event["event"] for event in events]},
                }
            }
        )
        return transcript


def test_continues_without_record_when_record_cannot_be_created(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        (workspace / ".mcp-console").write_text("occupied", encoding="utf-8")
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            current_directory=workspace,
        )
        client._initialize_and_list_tools()

        client.send(r="echo echo")
        client.send(control="restart")

        client._request("tools/call", name="missing", arguments={})
        assert client.transcript[-1]["error"] == {
            "code": -32602,
            "message": "tool not found",
        }, client.transcript[-1]
        assert (workspace / ".mcp-console").read_text(encoding="utf-8") == "occupied"
        transcript, standard_error = client._finish_with_standard_error()
        assert standard_error.count("\n") == 1, standard_error
        assert standard_error.startswith(
            "mcp-console: transcript recording disabled: failed to create "
        ), standard_error
        assert ".mcp-console/sessions" in standard_error, standard_error
        transcript.append(
            {
                "server stderr": (
                    "mcp-console: transcript recording disabled: "
                    "<run record creation failed>"
                )
            }
        )
        return transcript


def test_updates_quarto_without_rereading_journal(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            current_directory=workspace,
        )
        finished = False
        journal_read_disabled = False
        try:
            client._initialize_and_list_tools()
            client.send(r="echo first")

            session = next((workspace / ".mcp-console" / "sessions").iterdir())
            journal = session / "internal" / "events.jsonl"
            journal.chmod(0o200)
            journal_read_disabled = True

            client.send(python="echo second")
            quarto = (session / "transcript.qmd").read_text(encoding="utf-8")

            journal.chmod(0o600)
            journal_read_disabled = False
            assert "```{r}\necho first\n```" in quarto, quarto
            assert "```{python}\necho second\n```" in quarto, quarto

            transcript = client._finish()
            transcript.append(
                {
                    "quarto projection": {
                        "updated from incremental state": True,
                        "journal reopened for reading": False,
                    }
                }
            )
            finished = True
            return transcript
        finally:
            if journal_read_disabled:
                journal.chmod(0o600)
            if not finished:
                stop_client(client)


def test_records_tool_calls_and_images(binary: Path) -> TranscriptWithCompanions:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        environment, _ = r_test_environment()
        environment["RETICULATE_PYTHON"] = ""
        record_resolved_r_library(environment, workspace)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
            current_directory=workspace,
            umask=0,
        )
        client._initialize_and_list_tools()
        client.send(
            r="emit image",
            stdin="recorded stdin\n",
            requirements={"r": ["praise"]},
        )
        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        quarto_path = session / "transcript.qmd"
        quarto_before_python_requirement = quarto_path.read_text(encoding="utf-8")
        quarto_before_inode = quarto_path.stat().st_ino
        assert "    - praise" in quarto_before_python_requirement
        assert "transcript-fixture" not in quarto_before_python_requirement
        image_request_id = client.transcript[-1]["id"]
        invalid = client._request(
            "tools/call",
            name="send",
            arguments={"r": "1", "python": "1"},
            _meta={"progressToken": "record-me"},
        )
        client.send(requirements={"python": ["transcript-fixture"]})
        preparation_request_id = client.transcript[-1]["id"]
        preparation_result = client.transcript[-1]["result"]
        client._request("tools/call", name="missing", arguments={})

        sessions = list((workspace / ".mcp-console" / "sessions").iterdir())
        assert len(sessions) == 1, sessions
        session = sessions[0]
        journal_text = (session / "internal" / "events.jsonl").read_text(
            encoding="utf-8"
        )
        markdown_path = session / "transcript.md"
        markdown_text = markdown_path.read_text(encoding="utf-8")
        quarto_text = quarto_path.read_text(encoding="utf-8")
        assert PNG_1X1 not in journal_text, journal_text
        assert PNG_1X1 not in markdown_text, markdown_text
        assert PNG_1X1 not in quarto_text, quarto_text
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
        ], events
        run_id = events[0]["run_id"]
        assert run_id
        assert session.name == run_id, (session, run_id)
        assert events[0]["session"] == "default", events[0]
        assert Path(events[0]["working_directory"]).samefile(workspace), events[0]
        assert all(event["run_id"] == run_id for event in events), events
        assert all(event["schema_version"] == 1 for event in events), events
        assert [event["sequence"] for event in events] == list(range(1, 9)), events
        assert events[1]["call_id"] == events[2]["call_id"] == 1, events
        assert events[1]["request_id"] == image_request_id, events[1]
        assert events[1]["request"] == {
            "name": "send",
            "arguments": {
                "r": "emit image",
                "stdin": "recorded stdin\n",
                "requirements": {"r": ["praise"]},
            },
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
        assert events[4]["request_id"] == invalid["id"], events[4]
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
        assert events[6]["request_id"] == preparation_request_id, events[6]
        assert events[6]["request"] == {
            "name": "send",
            "arguments": {
                "requirements": {"python": ["transcript-fixture"]},
            },
        }, events[6]
        assert events[7]["result"] == preparation_result, events[7]
        assert [event["request"]["name"] for event in events if "request" in event] == [
            "send",
            "send",
            "send",
        ], events
        assert all(
            event.get("request", {}).get("name") != "missing" for event in events
        ), events

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
            for path in (
                session / "internal" / "events.jsonl",
                markdown_path,
                quarto_path,
                image_path,
            )
        }
        assert set(file_modes.values()) == {0o600}, file_modes
        transcript = client._finish()

        for event in events:
            assert event["at"].endswith("Z"), event
            datetime.fromisoformat(event["at"])
            event["at"] = "<UTC timestamp>"
            event["run_id"] = "<run ID>"
            if "request_id" in event:
                event["request_id"] = "<request ID>"
        events[0]["working_directory"] = "<workspace>"
        assert journal_text.endswith("\n"), journal_text
        assert markdown_text.endswith("\n"), markdown_text
        assert quarto_text.endswith("\n"), quarto_text
        assert "```{r}\nemit image\n```" in quarto_text
        assert quarto_path.stat().st_ino != quarto_before_inode
        assert "    - praise" in quarto_text
        assert "    - transcript-fixture" in quarto_text
        assert "python-version:" not in quarto_text
        assert all(
            excluded not in quarto_text
            for excluded in (
                "recorded stdin",
                "before image",
                "Artifact 1",
                "Result for call",
            )
        ), quarto_text

        return TranscriptWithCompanions(
            transcript=transcript,
            companions={
                "events.yaml": [
                    events,
                    {
                        "produced session": {
                            "root": ".mcp-console/sessions/<run ID>",
                            "files": [
                                "internal/events.jsonl",
                                "transcript.md",
                                "transcript.qmd",
                                "artifacts/call-000001-image-000001.png",
                            ],
                        }
                    },
                ],
            },
        )


def test_disables_recording_after_transcript_failure(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        client.send(r="echo echo")
        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        artifacts = session / "artifacts"
        artifacts.rmdir()
        artifacts.write_text("not a directory", encoding="utf-8")

        client.send(r="emit image")
        image_result = client.transcript[-1]["result"]
        assert image_result == {
            "content": [
                {"type": "text", "text": "before image\n"},
                {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                {"type": "text", "text": "after image\n"},
            ],
            "isError": False,
        }, image_result

        journal = session / "internal" / "events.jsonl"
        journal_after_failure = journal.read_text(encoding="utf-8")
        events = [json.loads(line) for line in journal_after_failure.splitlines()]
        assert [event["event"] for event in events] == [
            "session_started",
            "tool_call",
            "tool_result",
            "tool_call",
        ], events
        assert journal_after_failure.endswith("\n"), journal_after_failure

        client.send(r="echo echo")
        assert journal.read_text(encoding="utf-8") == journal_after_failure

        transcript, standard_error = client._finish_with_standard_error()
        assert standard_error.count("\n") == 1, standard_error
        assert standard_error.startswith(
            "mcp-console: transcript recording disabled: failed to create "
        ), standard_error
        assert "/artifacts/" in standard_error, standard_error
        transcript.append(
            {
                "journal after failure": [event["event"] for event in events],
                "complete final line": True,
                "post-failure append": False,
                "server stderr": (
                    "mcp-console: transcript recording disabled: "
                    "<artifact persistence failed>"
                ),
            }
        )
        return transcript


def test_flushes_calls_and_keeps_unpolled_images(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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
        client._initialize_and_list_tools()

        waiting = client._start_send(r="complete after release")
        started = wait_for_marker(
            temporary,
            "zod-evaluation-started",
            client,
        )
        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        journal = session / "internal" / "events.jsonl"
        markdown = session / "transcript.md"
        quarto = session / "transcript.qmd"
        before_release = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        assert [event["event"] for event in before_release] == [
            "session_started",
            "tool_call",
        ], before_release
        before_release_markdown = markdown.read_text(encoding="utf-8")
        before_release_quarto = quarto.read_text(encoding="utf-8")
        assert "## Call 1: R" in before_release_markdown
        assert "complete after release" in before_release_markdown
        assert "## Result for call 1" not in before_release_markdown
        assert "```{r}\ncomplete after release\n```" in before_release_quarto
        markdown_inode = markdown.stat().st_ino
        quarto_inode = quarto.stat().st_ino

        (started.parent / "zod-release-evaluation").touch()
        client._receive(waiting)
        after_release = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        assert [event["event"] for event in after_release] == [
            "session_started",
            "tool_call",
            "tool_result",
        ], after_release
        after_release_markdown = markdown.read_text(encoding="utf-8")
        after_release_quarto = quarto.read_text(encoding="utf-8")
        assert after_release_markdown.startswith(before_release_markdown)
        assert markdown.stat().st_ino == markdown_inode
        assert "## Result for call 1" in after_release_markdown
        assert "zod: complete after release" in after_release_markdown
        assert after_release_quarto == before_release_quarto
        assert quarto.stat().st_ino == quarto_inode

        client.send(
            r="emit image before completion",
            timeout_ms=0,
        )
        assert client.transcript[-1]["result"] == {
            "content": [
                {"type": "text", "text": "\n[running; poll with an empty send]"}
            ],
            "isError": False,
        }, client.transcript[-1]
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "<leading newline>[running; poll with an empty send]"
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
        unpolled_markdown = markdown.read_text(encoding="utf-8")
        unpolled_quarto = quarto.read_text(encoding="utf-8")
        assert unpolled_markdown.startswith(after_release_markdown)
        assert markdown.stat().st_ino == markdown_inode
        assert f"[Artifact {artifact['artifact_id']} from call 2]" in unpolled_markdown
        assert artifact["path"] in unpolled_markdown
        assert unpolled_quarto.startswith(after_release_quarto)
        assert "```{r}\nemit image before completion\n```" in unpolled_quarto
        assert quarto.stat().st_ino != quarto_inode
        unpolled_quarto_inode = quarto.stat().st_ino

        (image_started.parent / "zod-release-image-completion").touch()
        client.send(timeout_ms=3_000)
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
        polled_markdown = markdown.read_text(encoding="utf-8")
        polled_quarto = quarto.read_text(encoding="utf-8")
        assert polled_markdown.startswith(unpolled_markdown)
        assert markdown.stat().st_ino == markdown_inode
        assert "## Call 3: Poll" in polled_markdown
        assert "## Result for call 3" in polled_markdown
        assert polled_quarto == unpolled_quarto
        assert quarto.stat().st_ino == unpolled_quarto_inode

        transcript = client._finish()
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
                    "Markdown projection": {
                        "live before result": True,
                        "each snapshot retained as an exact prefix": True,
                        "inode retained": True,
                    },
                    "Quarto projection": "source cells only",
                }
            }
        )
        return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
