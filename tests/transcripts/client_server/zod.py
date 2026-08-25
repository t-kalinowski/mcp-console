#!/usr/bin/env -S uv run --script

import base64
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import (
    FifoCheckpoint,
    McpClient,
    Transcript,
    TranscriptWithCompanion,
    code,
    r_test_environment,
    run_this_suite,
    stop_client,
)

PLATFORMS = {"darwin"}
LARGE_OUTPUT_SIZE = 2 * 1024 * 1024
PENDING_TEXT_BUDGET = 8 * 1024 * 1024
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)


def build_killpg_denial_interposer(directory: Path) -> Path:
    source = directory / "deny-killpg.c"
    library = directory / "deny-killpg.dylib"
    source.write_text(
        r"""
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <sys/types.h>
#include <unistd.h>

static pid_t denied_process_group = 0;
static int added_late_member = 0;
static pid_t late_member = 0;
static int killpg_count = 0;

static void write_pid_marker(const char *name, pid_t process_id) {
    const char *marker = getenv(name);
    if (marker == NULL) {
        return;
    }
    int descriptor = open(marker, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (descriptor >= 0) {
        dprintf(descriptor, "%d\n", process_id);
        close(descriptor);
    }
}

static void write_member_marker(pid_t process_id, pid_t process_group) {
    const char *marker = getenv("MCP_CONSOLE_TEST_LATE_MEMBER_MARKER");
    if (marker == NULL) {
        return;
    }
    int descriptor = open(marker, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (descriptor >= 0) {
        dprintf(descriptor, "%d %d\n", process_id, process_group);
        close(descriptor);
    }
}

static int deny_killpg(pid_t process_group, int signal) {
    if (signal == SIGKILL
        && getenv("MCP_CONSOLE_TEST_KILLPG_COUNT_MARKER") != NULL) {
        const char *marker = getenv("MCP_CONSOLE_TEST_KILLPG_COUNT_MARKER");
        int descriptor = open(marker, O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (descriptor >= 0) {
            killpg_count += 1;
            dprintf(descriptor, "%d %d\n", killpg_count, process_group);
            close(descriptor);
        }
    }
    if (signal == SIGKILL
        && getenv("MCP_CONSOLE_TEST_KILLPG_MARKER") != NULL) {
        denied_process_group = process_group;
        write_pid_marker("MCP_CONSOLE_TEST_KILLPG_MARKER", process_group);
        errno = EPERM;
        return -1;
    }
    if (signal == SIGINT
        && getenv("MCP_CONSOLE_TEST_DENIED_SIGINT") != NULL) {
        write_pid_marker("MCP_CONSOLE_TEST_DENIED_SIGINT", process_group);
        errno = EPERM;
        return -1;
    }
    return (int)syscall(SYS_kill, -process_group, signal);
}

static pid_t add_process_group_member(pid_t process_group) {
    int descriptors[2];
    if (pipe(descriptors) != 0) {
        return -1;
    }

    pid_t member = fork();
    if (member < 0) {
        close(descriptors[0]);
        close(descriptors[1]);
        return -1;
    }
    if (member == 0) {
        close(descriptors[0]);
        if (setpgid(0, process_group) != 0) {
            _exit(1);
        }
        pid_t process_id = getpid();
        if (write(descriptors[1], &process_id, sizeof(process_id))
            != sizeof(process_id)) {
            _exit(1);
        }
        close(descriptors[1]);
        for (;;) {
            pause();
        }
    }

    close(descriptors[1]);
    pid_t acknowledged_member = 0;
    ssize_t bytes_read;
    do {
        bytes_read = read(
            descriptors[0],
            &acknowledged_member,
            sizeof(acknowledged_member)
        );
    } while (bytes_read < 0 && errno == EINTR);
    int read_error = bytes_read < 0 ? errno : EIO;
    close(descriptors[0]);

    if (bytes_read != sizeof(acknowledged_member)
        || acknowledged_member != member) {
        syscall(SYS_kill, member, SIGKILL);
        while (waitpid(member, NULL, 0) < 0 && errno == EINTR) {
        }
        errno = read_error;
        return -1;
    }
    return member;
}

static pid_t getpgid_and_add_member(pid_t process_id) {
    pid_t process_group = (pid_t)syscall(SYS_getpgid, process_id);
    // Rust rechecks group membership only after taking its kernel snapshot.
    // Join the group here so a one-pass fallback cannot observe this child.
    if (process_group == denied_process_group && !added_late_member) {
        added_late_member = 1;
        pid_t member = add_process_group_member(process_group);
        if (member < 0) {
            return -1;
        }
        late_member = member;
        write_member_marker(member, process_group);
    }
    return process_group;
}

static int kill_and_reap_late_member(pid_t process_id, int signal) {
    int result = (int)syscall(SYS_kill, process_id, signal);
    int signal_error = errno;
    if (result == 0 && signal == SIGKILL && process_id == late_member) {
        // Keep the final assertion independent of launchd's orphan reaping.
        int status = 0;
        pid_t waited;
        do {
            waited = waitpid(process_id, &status, 0);
        } while (waited < 0 && errno == EINTR);
        if (waited != process_id) {
            return -1;
        }
        write_pid_marker("MCP_CONSOLE_TEST_LATE_MEMBER_REAP_MARKER", process_id);
        late_member = 0;
    }
    errno = signal_error;
    return result;
}

__attribute__((constructor))
static void remove_interposer_from_child_environment(void) {
    unsetenv("DYLD_INSERT_LIBRARIES");
}

__attribute__((used))
static struct {
    const void *replacement;
    const void *replacee;
} interposers[] __attribute__((section("__DATA,__interpose"))) = {
    {(const void *)&deny_killpg, (const void *)&killpg},
    {(const void *)&getpgid_and_add_member, (const void *)&getpgid},
    {(const void *)&kill_and_reap_late_member, (const void *)&kill},
};
""".removeprefix("\n"),
        encoding="utf-8",
    )
    subprocess.run(
        ["cc", "-dynamiclib", "-o", library, source],
        check=True,
        capture_output=True,
        text=True,
    )
    return library


def record_resolved_r_library(environment: dict[str, str], directory: Path) -> None:
    real_ir = shutil.which("ir", path=environment.get("PATH"))
    assert real_ir is not None, "ir is required"
    identity = directory / "resolved-r-library"
    fake_bin = directory / "fixture-r-bin"
    fake_bin.mkdir()
    ir = fake_bin / "ir"
    ir.write_text(
        code(r"""
            #!/bin/sh

            set -eu
            if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
              exec "$MCP_CONSOLE_TEST_REAL_IR" "$@"
            fi
            if [ -n "${MCP_CONSOLE_TEST_R_RESOLUTION_FAILURE:-}" ] &&
              [ -e "$MCP_CONSOLE_TEST_R_RESOLUTION_FAILURE" ]; then
              printf 'fixture R resolver failed\n' >&2
              exit 1
            fi
            library=$("$MCP_CONSOLE_TEST_REAL_IR" "$@")
            printf '%s' "$library" > "$MCP_CONSOLE_TEST_R_LIBRARY_IDENTITY"
            printf '%s' "$library"
            """),
        encoding="utf-8",
    )
    ir.chmod(0o755)
    path = environment.get("PATH")
    assert path is not None, "PATH is required"
    environment["PATH"] = os.pathsep.join((str(fake_bin), path))
    environment["MCP_CONSOLE_TEST_REAL_IR"] = real_ir
    environment["MCP_CONSOLE_TEST_R_LIBRARY_IDENTITY"] = str(identity)


def test_routes_send_over_sideband(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(r="echo hello")
    assert last_tool_text(client) == "zod: hello\n"
    client.send(python="echo precise 👩🏽‍💻")
    assert last_tool_text(client) == "zod python: precise 👩🏽‍💻\n"
    client.send(sql="echo two  spaces")
    assert last_tool_text(client) == "zod sql: two  spaces\n"
    return client._finish()


def test_projects_console_kinds(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    result = client.send(r="emit console kinds")
    assert result == {
        "content": [
            {
                "type": "text",
                "text": "zod output\nzod diagnostic\n",
            }
        ],
        "isError": False,
    }, result
    return client._finish()


def test_returns_worker_images(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(r="emit image")
    result = client.transcript[-1]["result"]
    assert result == {
        "content": [
            {"type": "text", "text": "before image\n"},
            {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
            {"type": "text", "text": "after image\n"},
        ],
        "isError": False,
    }, result
    return client._finish()


def test_materializes_records_only_for_console_use(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        unused_workspace = temporary / "unused"
        unused_workspace.mkdir()
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            current_directory=unused_workspace,
        )
        client._initialize_and_list_tools()
        assert not (unused_workspace / ".mcp-console").exists(), unused_workspace
        client._request("tools/call", name="missing", arguments={})
        assert not (unused_workspace / ".mcp-console").exists(), unused_workspace
        transcript = client._finish()
        assert not (unused_workspace / ".mcp-console").exists(), unused_workspace

        materialized_by = {}
        for tool in ("send", "session"):
            workspace = temporary / tool
            workspace.mkdir()
            client = McpClient(
                binary,
                ("serve", "--worker", str(zod)),
                current_directory=workspace,
            )
            client._initialize_and_list_tools()
            assert not (workspace / ".mcp-console").exists(), workspace
            if tool == "send":
                client.send(r="echo echo")
            else:
                client.session(action="restart")

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
            assert events[1]["request"]["name"] == tool, events[1]
            materialized_by[tool] = [event["event"] for event in events]
            client._finish()

        transcript.append(
            {
                "recording": {
                    "initialization and unknown tool only": "absent",
                    "materialized by": materialized_by,
                }
            }
        )
        return transcript


def test_continues_without_record_when_record_cannot_be_created(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
        client.session(action="restart")

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


def test_records_tool_calls_and_images(binary: Path) -> TranscriptWithCompanion:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
        image_request_id = client.transcript[-1]["id"]
        invalid = client._request(
            "tools/call",
            name="send",
            arguments={"r": "1", "python": "1"},
            _meta={"progressToken": "record-me"},
        )
        client.session(
            action="prepare",
            requirements={"python": ["transcript-fixture"]},
        )
        preparation_request_id = client.transcript[-1]["id"]
        session_result = client.transcript[-1]["result"]
        client._request("tools/call", name="missing", arguments={})

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
            "name": "session",
            "arguments": {
                "action": "prepare",
                "requirements": {"python": ["transcript-fixture"]},
            },
        }, events[6]
        assert events[7]["result"] == session_result, events[7]
        assert [event["request"]["name"] for event in events if "request" in event] == [
            "send",
            "send",
            "session",
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
            for path in (session / "internal" / "events.jsonl", image_path)
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

        return TranscriptWithCompanion(
            transcript=transcript,
            companion_name="events",
            companion=[
                events,
                {
                    "produced session": {
                        "root": ".mcp-console/sessions/<run ID>",
                        "files": [
                            "internal/events.jsonl",
                            "artifacts/call-000001-image-000001.png",
                        ],
                    }
                },
            ],
        )


def test_disables_recording_after_transcript_failure(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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

        waiting = client._start_send(
            r="complete after release",
            timeout_ms=3_000,
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
                }
            }
        )
        return transcript


def test_custom_worker_skips_managed_python_preflight(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment["R_HOME"] = "/mcp-console-custom-worker-must-not-run-rscript"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
        environment,
    )
    client._initialize_and_list_tools()
    # fmt: python
    python = code(r"""
        echo echo
        """).removesuffix("\n")
    client.send(python=python)
    result = client.session(
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "Python requirements are unavailable with a custom worker"
    )
    result = client.session(
        action="restart",
        requirements={"python": ["py-yaml12"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "Python requirements are unavailable with a custom worker"
    )
    result = client.send(
        r="echo must not run",
        requirements={"python": ["py-yaml12"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "Python requirements are unavailable with a custom worker"
    )
    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"
    client.send()
    assert last_tool_text(client) == "\n[idle]"
    return client._finish()


def test_custom_worker_starts_without_home(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment = os.environ.copy()
    environment.pop("HOME", None)
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
        environment,
    )
    client._initialize_and_list_tools()

    client.send(sql="echo echo")
    assert last_tool_text(client) == "zod sql: echo\n"

    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"
    return client._finish()


def test_custom_worker_prepares_r_and_duckdb_requirements(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        isolated_library = temporary_path / "isolated-library"
        isolated_library.mkdir()
        environment["R_LIBS"] = str(isolated_library)
        environment["R_LIBS_SITE"] = str(isolated_library)
        environment["R_LIBS_USER"] = str(isolated_library)
        environment["TMPDIR"] = temporary
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()
        client.send(r="echo echo")

        client.session(
            action="prepare",
            requirements={"r": ["praise"]},
        )
        assert last_tool_text(client) == "[prepared]"

        client.session(
            action="prepare",
            requirements={"duckdb": ["json"]},
        )
        assert last_tool_text(client) == "[prepared]"

        client.send(r="report managed R requirement")
        assert last_tool_text(client) == "zod R requirement: prepared=true\n"

        client.send(r="fail next r preparation after output")
        assert last_tool_text(client) == "[done]"
        result = client.send(
            r="echo failed preparation cell ran",
            requirements={"r": ["zeallot"]},
        )
        assert result["isError"] is True, result
        assert result["content"] == [
            {"type": "text", "text": "before failed preparation\n"},
            {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
            {
                "type": "text",
                "text": (
                    "\nzod rejected R preparation; further requirement changes "
                    "are unavailable until session restart"
                ),
            },
        ], result

        assert client.temporary_directory is not None
        workspace = Path(client.temporary_directory.name)
        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        events = [
            json.loads(line)
            for line in (session / "internal" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        artifact = events[-2]
        recorded_result = events[-1]
        assert artifact["event"] == "artifact_created", artifact
        assert recorded_result["event"] == "tool_result", recorded_result
        assert artifact["call_id"] == recorded_result["call_id"], events[-2:]
        assert recorded_result["result"]["content"][1] == {
            "type": "image",
            "artifactId": artifact["artifact_id"],
            "path": artifact["path"],
            "mimeType": "image/png",
        }, recorded_result
        assert (session / artifact["path"]).read_bytes() == base64.b64decode(PNG_1X1)

        client.send(r="emit output and image before completion", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        image_started = wait_for_marker(
            temporary_path,
            "zod-image-evaluation-started",
            client,
        )
        (image_started.parent / "zod-release-image").touch()
        wait_for_marker(temporary_path, "zod-image-processed", client)
        try:
            result = client.send(
                r="echo active restart-required cell ran",
                requirements={"r": ["cli"]},
            )
            assert result == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "worker is already evaluating a cell; poll it before "
                            "preparing requirements"
                        ),
                    }
                ],
                "isError": True,
            }, result

            client.send(timeout_ms=0)
            assert client.transcript[-1]["result"] == {
                "content": [
                    {"type": "text", "text": "before pending image\n"},
                    {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                    {
                        "type": "text",
                        "text": "after pending image\n\n[running; poll with an empty send]",
                    },
                ],
                "isError": False,
            }, client.transcript[-1]
        finally:
            (image_started.parent / "zod-release-image-completion").touch()
        client.send(timeout_ms=3_000)
        assert last_tool_text(client) == "[done]"

        result = client.send(
            r="echo restart-required cell ran",
            requirements={"r": ["cli"]},
        )
        assert result == {
            "content": [
                {
                    "type": "text",
                    "text": "requirements require session restart; cell was not run",
                }
            ],
            "isError": True,
        }, result
        client.send(r="echo worker remains usable")
        assert last_tool_text(client) == "zod: worker remains usable\n"

        result = client.send(r="report managed python activation")
        assert result["isError"] is True, result
        failure = result["content"][0]["text"]
        assert "custom worker reported a managed Python activation" in failure, failure
        return client._finish()


def test_cancelled_combined_preparation_failure_is_reclaimed_by_restart(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    ordered_ir = (
        Path(__file__).resolve().parents[2] / "fixtures" / "ordered_retirement_ir"
    )
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        library = temporary_path / "resolved-library"
        library.mkdir()
        fake_bin = temporary_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "ir").symlink_to(ordered_ir)
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        environment["TMPDIR"] = temporary
        environment["MCP_CONSOLE_TEST_IR_COUNTER"] = str(temporary_path / "ir-counter")
        environment["MCP_CONSOLE_TEST_IR_LIBRARIES"] = str(library)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        finished = False
        try:
            client._initialize_and_list_tools()
            client.send(r="echo worker ready")
            assert last_tool_text(client) == "zod: worker ready\n"
            client.send(r="fail next r preparation after large output")
            assert last_tool_text(client) == "[done]"

            failed = client._start_send(
                r="echo cancelled cell ran",
                requirements={"r": ["praise"]},
            )
            # The 4 MiB response cannot fit in the stdout pipe. Once its first
            # bytes are readable, cancellation can overtake the blocked write.
            readable, _, _ = select.select([client.stdout], [], [], 10)
            assert readable, "combined preparation failure did not reach MCP output"
            client._notify(
                "notifications/cancelled",
                requestId=failed["id"],
                reason="acceptance test cancelled the combined send",
            )
            cancellation = client.transcript[-1]["input"]["params"]
            assert cancellation["requestId"] == failed["id"], cancellation
            cancellation["requestId"] = "<request ID>"

            # Flushing a second 4 MiB message cannot finish until the server
            # asks the ordered input transport for the message after the
            # cancellation. Keep stdout blocked until that causal barrier.
            barrier_size = 2 * LARGE_OUTPUT_SIZE
            client._notify(
                "notifications/acceptance-test-barrier",
                padding="b" * barrier_size,
            )
            barrier = client.transcript[-1]["input"]["params"]
            assert len(barrier["padding"]) == barrier_size, barrier
            barrier["padding"] = f"<input barrier: {barrier_size} bytes>"
            restart = client._start_session(action="restart")

            discarded = json.loads(client.stdout.readline())
            assert discarded.pop("jsonrpc", None) == "2.0", discarded
            assert discarded.pop("id", None) == failed["id"], discarded
            assert discarded["result"]["isError"] is True, discarded
            assert failed.keys() == {"id", "send"}, failed

            client._receive(restart)
            restarted = restart["result"]
            large_output = "x" * (2 * LARGE_OUTPUT_SIZE)
            assert restarted == {
                "content": [
                    {
                        "type": "text",
                        "text": "before failed preparation\n" + large_output,
                    },
                    {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                    {
                        "type": "text",
                        "text": (
                            "\nzod rejected R preparation; further requirement "
                            "changes are unavailable until session restart\n"
                            "[worker stopped: in-memory state lost]\n"
                            "[starting new worker]\n[idle]"
                        ),
                    },
                ],
                "isError": True,
            }, restarted
            assert all(
                "zod: cancelled cell ran" not in content.get("text", "")
                for content in restarted["content"]
            ), restarted
            restarted["content"][0]["text"] = (
                f"before failed preparation\n<large output: {len(large_output)} bytes>"
            )

            client.send()
            assert last_tool_text(client) == "\n[idle]"
            transcript = client._finish()
            finished = True
            return transcript
        finally:
            if not finished:
                stop_client(client)


def test_custom_worker_reports_idle_input_before_preparation_failure(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        isolated_library = temporary_path / "isolated-library"
        isolated_library.mkdir()
        environment["R_LIBS"] = str(isolated_library)
        environment["R_LIBS_SITE"] = str(isolated_library)
        environment["R_LIBS_USER"] = str(isolated_library)
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()
        client.send(r="request input while idle")
        output = last_tool_text(client)
        assert output == '[input requested: "idle> "]\n', repr(output)

        result = client.session(
            action="prepare",
            requirements={"r": ["praise"]},
        )
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            '[idle R callback requested input "idle> " during requirement '
            "preparation; collect callback input with send before preparing requirements]\n"
            "[worker terminated by signal 9]\n"
            "[worker stopped: in-memory state lost]"
        ), result
        return client._finish()


def test_custom_worker_resolves_idle_activity_before_preparation(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        isolated_library = temporary_path / "isolated-library"
        isolated_library.mkdir()
        environment["R_LIBS"] = str(isolated_library)
        environment["R_LIBS_SITE"] = str(isolated_library)
        environment["R_LIBS_USER"] = str(isolated_library)
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()
        client.send(r="resolve python while idle")
        assert last_tool_text(client) == "[done]"

        client.session(
            action="prepare",
            requirements={"r": ["praise"]},
        )
        assert last_tool_text(client) == "[prepared]"
        client.send(r="report managed R requirement")
        assert last_tool_text(client) == "zod R requirement: prepared=true\n"
        return client._finish()


def test_combined_requirements_keep_idle_output_as_one_prelude(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        failure = temporary_path / "fail-r-resolution"
        environment["TMPDIR"] = temporary
        environment["MCP_CONSOLE_TEST_R_RESOLUTION_FAILURE"] = str(failure)
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()
        expose_idle_sideband_output(client, temporary_path, "combined-requirements")

        client.send(
            r="echo combined cell",
            requirements={"r": ["praise"]},
        )
        assert last_tool_text(client) == (
            "zod background sideband\n"
            "[output produced while idle]\n"
            "zod: combined cell\n"
        )
        client.send()
        assert last_tool_text(client) == "\n[idle]"

        expose_idle_sideband_output(
            client,
            temporary_path,
            "combined-requirements-failure",
        )
        failure.touch()
        result = client.send(
            r="echo failed resolver cell ran",
            requirements={"r": ["cli"]},
        )
        assert result == {
            "content": [
                {"type": "text", "text": "idle before failure image\n"},
                {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                {
                    "type": "text",
                    "text": (
                        "idle after failure image\n"
                        "[output produced while idle]\n"
                        "R package resolution failed with exit status: 1: "
                        "fixture R resolver failed"
                    ),
                },
            ],
            "isError": True,
        }, result
        failure.unlink()
        client.send(r="echo worker still usable")
        assert last_tool_text(client) == "zod: worker still usable\n"
        return client._finish()


def test_custom_worker_resolves_idle_activity_before_evaluation(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
        environment,
    )
    client._initialize_and_list_tools()
    client.send(r="resolve python while idle")
    assert last_tool_text(client) == "[done]", repr(last_tool_text(client))

    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"

    client.send(r="request input while idle")
    assert last_tool_text(client) == '[input requested: "idle> "]\n'
    client.send(r="echo echo", stdin="continue\n")
    assert last_tool_text(client) == "zod: echo\n"
    return client._finish()


def test_custom_worker_restart_prepares_r_and_duckdb_requirements(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        isolated_library = temporary_path / "isolated-library"
        isolated_library.mkdir()
        environment["R_LIBS"] = str(isolated_library)
        environment["R_LIBS_SITE"] = str(isolated_library)
        environment["R_LIBS_USER"] = str(isolated_library)
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()

        client.session(
            action="restart",
            requirements={"r": ["praise"], "duckdb": ["json"]},
        )
        assert last_tool_text(client) == "[starting new worker]\n[idle]"

        client.send(r="report managed R requirement")
        assert last_tool_text(client) == "zod R requirement: prepared=true\n"
        return client._finish()


def test_captures_worker_stdout(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(r="emit stdout")
    output = last_tool_text(client)
    assert_large_output(output, "zod stdout 👩🏽‍💻\n")
    client.transcript[-1]["result"]["content"][0]["text"] = (
        "zod stdout 👩🏽‍💻\n<large output>\n"
    )
    return client._finish()


def test_compacts_split_terminal_redraws(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="emit terminal redraws")
    assert (
        last_tool_text(client) == "ordinary stdout\r\n\x1b[32mstdout café 100%\x1b[0m"
    )
    return client._finish()


def test_compacts_stdout_and_stderr_independently(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="emit independent stdout stderr redraws")
    output = last_tool_text(client)
    assert sorted(output.splitlines()) == ["stderr final", "stdout final"], output
    client.transcript[-1]["result"]["content"][0]["text"] = (
        "<stdout and stderr final lines>\n"
    )
    client.transcript[-1]["transcript_normalization"] = {
        "target": "result.content[0].text",
        "cross_source_position": "omitted",
    }
    return client._finish()


def test_finalization_preserves_volatile_output_order(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="finalize ordered redraw")
    assert last_tool_text(client) == "ordinary\nprogresserror\n"
    return client._finish()


def test_oversized_progress_line_falls_back_without_loss(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="oversized progress line")
    output = last_tool_text(client)
    assert output == ("x" * ((64 * 1024) + 7)) + "\r"
    client.transcript[-1]["result"]["content"][0]["text"] = (
        "<oversized progress line>\r"
    )
    client.transcript[-1]["transcript_normalization"] = {
        "target": "result.content[0].text",
        "replacements": {"progress_line": "<oversized progress line>"},
    }
    return client._finish()


def test_bounds_combining_data_in_progress_frames(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="oversized combining progress frame")
    output = last_tool_text(client)
    combining = "a" + ("\u0301" * (((16 * 1024) // 2) + 1))
    assert output == combining + "\rfinal\n"
    client.transcript[-1]["result"]["content"][0]["text"] = (
        "<bounded combining progress frame>\rfinal\n"
    )
    client.transcript[-1]["transcript_normalization"] = {
        "target": "result.content[0].text",
        "replacements": {
            "progress_line": "<bounded combining progress frame>",
        },
    }
    return client._finish()


def test_compacts_independent_streams_across_polls_and_completion(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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

        client.send(r="redraw across polls", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        stage_1 = wait_for_marker(
            temporary_path,
            "zod-redraw-stage-1",
            client,
        )

        client.send(timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        client.send(timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"

        (stage_1.parent / "zod-release-redraw-stage-2").touch()
        stage_2 = wait_for_marker(
            temporary_path,
            "zod-redraw-stage-2",
            client,
        )
        client.send(timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"

        (stage_2.parent / "zod-release-redraw-completion").touch()
        client.send(timeout_ms=3_000)
        assert last_tool_text(client) == "output 100%\ndiagnostic done\n"
        return client._finish()


def test_restart_flushes_and_resets_volatile_output(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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

        client.send(r="redraw until restart", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        wait_for_marker(
            temporary_path,
            "zod-redraw-ready-for-restart",
            client,
        )

        client.session(action="restart")
        assert last_tool_text(client) == (
            "latest restart progress\n"
            "[active evaluation stopped by session restart request]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )

        client.send(r="echo fresh")
        assert last_tool_text(client) == "zod: fresh\n"
        return client._finish()


def test_restart_resets_idle_volatile_output(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
        expose_idle_sideband_output(client, temporary_path, "idle-redraw")

        client.session(action="restart")
        assert last_tool_text(client) == (
            "idle redraw final\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )

        client.send(r="echo fresh")
        assert last_tool_text(client) == "zod: fresh\n"
        return client._finish()


def test_many_redraws_do_not_consume_pending_output_limits(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="stress redraws")
    assert last_tool_text(client) == "stress final\nuseful output\n"
    return client._finish()


def test_preserves_invalid_raw_output_when_worker_exits(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    for stream in ("stdout", "stderr"):
        client.send(r=f"exit after invalid {stream}")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        failure = (
            "\n[worker sideband read failed: worker sideband closed]\n"
            "[worker exited with status 86]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )
        output = result["content"][0]["text"]
        assert output.endswith(failure), output[-200:]
        prefix = f"zod invalid {stream}: � trailing: �"
        raw_output = output.removesuffix(failure)
        marker_prefix = f"zod expected {stream} crash tail: "
        raw_output, tail_size = remove_length_marker(raw_output, marker_prefix)
        assert raw_output == large_output(prefix) + ("z" * tail_size), (
            f"worker crash lost {stream} bytes"
        )
        result["content"][0]["text"] = prefix + "<large output>" + failure

    return client._finish()


def test_preserves_raw_output_during_malformed_sideband_failure(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    for stream in ("stdout", "stderr"):
        client.send(r=f"malformed sideband after {stream}")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        output = result["content"][0]["text"]
        marker_prefix = f"zod expected {stream} malformed tail: "
        output, tail_size = remove_length_marker(output, marker_prefix)
        prefix = f"zod malformed {stream}: "
        raw = large_output(prefix) + ("z" * tail_size)
        failure_start = output.find("[worker sideband read failed: ")
        assert failure_start >= 0, output[-200:]
        failure_end = output.find("\n", failure_start)
        assert failure_end >= 0, output[-200:]
        failure = output[failure_start:failure_end]
        notices = [
            failure,
            "[worker terminated by signal 9]",
            "[worker stopped: in-memory state lost]",
            "[starting new worker]",
            "[idle]",
        ]
        assert output.count(raw) == 1, f"malformed frame lost {stream} bytes"
        assert all(output.count(notice) == 1 for notice in notices), repr(output)
        assert [output.index(notice) for notice in notices] == sorted(
            output.index(notice) for notice in notices
        ), repr(output)
        remainder = output.replace(raw, "")
        for notice in notices:
            remainder = remainder.replace(notice, "")
        assert not remainder.replace("\n", ""), repr(output)
        result["content"][0]["text"] = (
            f"{prefix}<large output>\n"
            "[worker sideband read failed: <invalid frame>]\n"
            "[worker terminated by signal 9]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n[idle]"
        )
        client.transcript[-1]["transcript_normalization"] = {
            "target": "result.content[0].text",
            "cross_source_position": "omitted",
            "replacements": {
                "large_output": "<large output>",
                "sideband_failure_detail": "<invalid frame>",
            },
        }

    transcript, standard_error = client._finish_with_standard_error()
    diagnostics = standard_error.splitlines()
    # Relay stderr is diagnostic-only and can be cut off when the server's
    # fail-safe stops a failed generation. The framed failure above is authoritative.
    assert len(diagnostics) <= 2, standard_error
    assert all(
        diagnostic.startswith("worker sideband read failed: ")
        for diagnostic in diagnostics
    ), standard_error
    return transcript


def test_preserves_raw_output_during_semantically_invalid_sideband_message(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="unexpected input receipt after stdout")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    output = result["content"][0]["text"]
    marker_prefix = "zod expected semantic tail: "
    output, tail_size = remove_length_marker(output, marker_prefix)
    prefix = "zod unexpected input receipt: "
    raw = large_output(prefix) + ("z" * tail_size)
    notices = [
        "[worker reported received input without requesting it]",
        "[worker terminated by signal 9]",
        "[worker stopped: in-memory state lost]",
        "[starting new worker]",
        "[idle]",
    ]
    assert output.count(raw) == 1, "semantic failure lost raw stdout bytes"
    assert all(output.count(notice) == 1 for notice in notices), repr(output)
    assert [output.index(notice) for notice in notices] == sorted(
        output.index(notice) for notice in notices
    ), repr(output)
    remainder = output.replace(raw, "")
    for notice in notices:
        remainder = remainder.replace(notice, "")
    assert not remainder.replace("\n", ""), repr(output)
    result["content"][0]["text"] = f"{prefix}<large output>\n" + "\n".join(notices)
    client.transcript[-1]["transcript_normalization"] = {
        "target": "result.content[0].text",
        "cross_source_position": "omitted",
        "replacements": {"large_output": "<large output>"},
    }
    return client._finish()


def test_drains_background_stderr_while_idle(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
        client.send(r="start background stderr")
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

        client.send(timeout_ms=0)
        output = last_tool_text(client)
        assert output.endswith("\n[idle]"), output[-100:]
        assert_large_output(
            output.removesuffix("\n[idle]"),
            "zod background stderr\n",
        )
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "zod background stderr\n<large output>\n[idle]"
        )
        return client._finish()


def test_times_out_and_polls_running_evaluation(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(r="echo echo")
    client.send(
        r="complete after timeout",
        timeout_ms=10,
    )
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "\n[running; poll with an empty send]", output
    client.send(timeout_ms=3_000)
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "zod: complete after timeout\n", output
    client.send(r="echo echo")
    return client._finish()


def test_drains_pending_sideband_output_while_running(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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

        client.send(r="emit output and image before completion", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        image_started = wait_for_marker(
            temporary_path,
            "zod-image-evaluation-started",
            client,
        )
        (image_started.parent / "zod-release-image").touch()
        wait_for_marker(temporary_path, "zod-image-processed", client)

        client.send(timeout_ms=0)
        result = client.transcript[-1]["result"]
        assert result == {
            "content": [
                {"type": "text", "text": "before pending image\n"},
                {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                {
                    "type": "text",
                    "text": "after pending image\n\n[running; poll with an empty send]",
                },
            ],
            "isError": False,
        }, result

        (image_started.parent / "zod-release-image-completion").touch()
        client.send(timeout_ms=3_000)
        assert last_tool_text(client) == "[done]"
        return client._finish()


def test_interrupts_running_worker_with_sigint(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="interrupt", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            wait_for_marker(
                temporary_path,
                "zod-interrupt-evaluation-started",
                client,
            )

            client.session(action="interrupt")
            assert last_tool_text(client) == "[interrupt sent]"
            wait_for_marker(temporary_path, "zod-sigint-received", client)

            client.send(timeout_ms=3_000)
            assert last_tool_text(client) == "zod interrupted\n"
            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)


def test_supervises_stopped_and_continued_workers(binary: Path) -> Transcript:
    wrapper = Path(__file__).resolve().parents[2] / "fixtures" / "stop_continue_zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(wrapper)),
            environment,
        )
        workers: list[tuple[int, int]] = []
        passed = False
        try:
            client._initialize_and_list_tools()
            evaluation = client._start_send(r="echo echo", timeout_ms=30_000)
            marker, worker_pid, worker_group = wait_for_stopped_worker(
                temporary_path,
                set(),
                workers,
                client,
            )

            interrupt = client._start_session(action="interrupt")
            readable, _, _ = select.select([client.stdout], [], [], 3)
            assert readable, "relay supervision did not answer the interrupt request"
            client._receive(interrupt)
            assert interrupt["result"] == {
                "content": [{"type": "text", "text": "[interrupt sent]"}],
                "isError": False,
            }, interrupt

            continue_stopped_worker(worker_pid, worker_group)
            wait_for_path(
                marker.with_name("zod-stop-continue-resumed"),
                "stopped worker to resume",
                client,
            )
            client._receive(evaluation)
            assert evaluation["result"] == {
                "content": [{"type": "text", "text": "zod: echo\n"}],
                "isError": False,
            }, evaluation

            restarted = client._start_session(action="restart")
            replacement_marker, replacement_pid, replacement_group = (
                wait_for_stopped_worker(
                    temporary_path,
                    {worker_pid},
                    workers,
                    client,
                )
            )
            assert replacement_group != worker_group, (
                "replacement reused the retiring process group"
            )
            wait_for_worker_retirement(worker_pid, worker_group, client)

            continue_stopped_worker(replacement_pid, replacement_group)
            wait_for_path(
                replacement_marker.with_name("zod-stop-continue-resumed"),
                "replacement worker to resume",
                client,
            )
            client._receive(restarted)
            assert restarted["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[worker stopped: in-memory state lost]\n"
                            "[starting new worker]\n"
                            "[idle]"
                        ),
                    }
                ],
                "isError": False,
            }, restarted

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                for recorded_pid, recorded_group in reversed(workers):
                    stop_recorded_worker(recorded_pid, recorded_group)
                stop_process(client.process)


def resolver_interrupt_permission_environment(
    temporary_path: Path,
) -> tuple[dict[str, str], Path, Path]:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    fake_bin = temporary_path / "bin"
    fake_bin.mkdir()
    fake_ir = fake_bin / "ir"
    fake_ir.write_text(
        code(r"""
            #!/bin/sh

            set -eu
            if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
              printf 'ir 0.4.0\n'
              exit 0
            fi
            printf '%s\n' "$$" > "$MCP_CONSOLE_TEST_RESOLVER_STARTED"
            exec /bin/sleep 30
            """),
        encoding="utf-8",
    )
    fake_ir.chmod(0o755)

    path = environment.get("PATH")
    assert path is not None, "PATH is required"
    environment["PATH"] = os.pathsep.join((str(fake_bin), path))
    environment["TMPDIR"] = str(temporary_path)
    denied_interrupt = temporary_path / "resolver-sigint-denied"
    resolver_started = temporary_path / "resolver-started"
    environment["MCP_CONSOLE_TEST_DENIED_SIGINT"] = str(denied_interrupt)
    environment["MCP_CONSOLE_TEST_RESOLVER_STARTED"] = str(resolver_started)
    # The interposer removes its loader variable after reaching the server, so
    # the resolver and Zod do not inherit it.
    environment["DYLD_INSERT_LIBRARIES"] = str(
        build_killpg_denial_interposer(temporary_path)
    )
    return environment, resolver_started, denied_interrupt


def test_reports_resolver_interrupt_permission_error(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment, resolver_started, denied_interrupt = (
            resolver_interrupt_permission_environment(temporary_path)
        )

        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        resolver_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            preparation = client._start_session(
                action="prepare",
                requirements={"r": ["blocked-resolver"]},
            )
            resolver_group = int(
                wait_for_marker(
                    temporary_path,
                    resolver_started.name,
                    client,
                ).read_text(encoding="utf-8")
            )
            assert resolver_group != os.getpgrp(), (
                "resolver did not enter a dedicated process group"
            )

            interrupt = client._start_session(action="interrupt")
            responses_returned = threading.Event()
            forced_stop = threading.Event()

            def stop_if_calls_block() -> None:
                if not responses_returned.wait(2):
                    forced_stop.set()
                    stop_process_group(resolver_group)

            watchdog = threading.Thread(target=stop_if_calls_block, daemon=True)
            watchdog.start()
            try:
                client._receive_many([preparation, interrupt])
            finally:
                responses_returned.set()
                watchdog.join()

            denied_group = int(
                wait_for_marker(
                    temporary_path,
                    denied_interrupt.name,
                    client,
                ).read_text(encoding="utf-8")
            )
            assert denied_group == resolver_group, (
                "SIGINT denial targeted a different process group"
            )
            wait_for_process_group_exit(resolver_group, client)
            assert not forced_stop.is_set(), (
                "resolver interrupt failure did not terminate both calls"
            )

            expected = {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "failed to interrupt R package resolver `ir`: "
                            "Operation not permitted (os error 1)"
                        ),
                    }
                ],
                "isError": True,
            }
            assert preparation["result"] == expected, preparation
            assert interrupt["result"] == expected, interrupt

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(resolver_group)
                stop_client(client)


def test_reports_runtime_r_resolver_interrupt_permission_error(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment, resolver_started, denied_interrupt = (
            resolver_interrupt_permission_environment(temporary_path)
        )
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        resolver_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            evaluation = client._start_send(
                r="report runtime R resolution failure",
            )
            resolver_group = int(
                wait_for_marker(
                    temporary_path,
                    resolver_started.name,
                    client,
                ).read_text(encoding="utf-8")
            )
            assert resolver_group != os.getpgrp(), (
                "resolver did not enter a dedicated process group"
            )

            interrupt = client._start_session(action="interrupt")
            responses_returned = threading.Event()
            forced_stop = threading.Event()

            def stop_if_calls_block() -> None:
                if not responses_returned.wait(2):
                    forced_stop.set()
                    stop_process_group(resolver_group)

            watchdog = threading.Thread(target=stop_if_calls_block, daemon=True)
            watchdog.start()
            try:
                client._receive_many([evaluation, interrupt])
            finally:
                responses_returned.set()
                watchdog.join()

            denied_group = int(
                wait_for_marker(
                    temporary_path,
                    denied_interrupt.name,
                    client,
                ).read_text(encoding="utf-8")
            )
            assert denied_group == resolver_group, (
                "SIGINT denial targeted a different process group"
            )
            wait_for_process_group_exit(resolver_group, client)
            assert not forced_stop.is_set(), (
                "resolver interrupt failure did not terminate both calls"
            )

            message = (
                "failed to interrupt R package resolver `ir`: "
                "Operation not permitted (os error 1)"
            )
            assert evaluation["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": f"zod R resolution failure: host: {message}\n",
                    }
                ],
                "isError": False,
            }, evaluation
            assert interrupt["result"] == {
                "content": [{"type": "text", "text": message}],
                "isError": True,
            }, interrupt

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(resolver_group)
                stop_client(client)


def test_accepts_idle_stdin(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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

            preparation = client._start_session(
                action="prepare",
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
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
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
    client.send(stdin="followup\n", timeout_ms=3_000)
    assert last_tool_text(client) == "zod stdin: followup\n"

    client.send(r="request input")
    assert last_tool_text(client) == '[input requested: "zod> "]\n[waiting for stdin]'
    client.send(stdin="")
    assert last_tool_text(client) == "\n[waiting for stdin]"
    client.send(stdin="prompted\n")
    assert last_tool_text(client) == "zod stdin: prompted\n"

    client.send(
        r="input without request then request input",
        stdin="first\n",
        timeout_ms=1_000,
    )
    assert (
        last_tool_text(client) == '[input requested: "second> "]\n[waiting for stdin]'
    )
    client.send(stdin="second\n")
    assert last_tool_text(client) == "zod stdin: first|second\n"

    client.send(r="echo echo", stdin="stale\n")
    assert last_tool_text(client) == "zod: echo\n"
    client.send(r="input without request")
    assert last_tool_text(client) == "zod stdin: stale\n"

    client.send(r="echo echo", stdin="x" * (128 * 1024), timeout_ms=1_000)
    client.transcript[-1]["send"]["stdin"] = "<large unread stdin>"
    assert last_tool_text(client) == "zod: echo\n"
    return client._finish()


def test_routes_same_call_stdin_to_direct_fd0(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="read fd 0 directly", stdin="direct café\n")
    assert last_tool_text(client) == "zod fd 0: 'direct café\\n'\n"
    return client._finish()


def test_preserves_unexposed_input_output(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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


def last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    return result["content"][0]["text"]


def test_bounds_pending_output_and_resets_after_completion(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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


def assert_large_output(output: str, prefix: str) -> None:
    expected = prefix + ("x" * LARGE_OUTPUT_SIZE)
    assert output.startswith(expected), (
        f"captured {len(output)} bytes without the complete {len(expected)}-byte payload"
    )
    barrier = output.removeprefix(expected)
    assert barrier and not barrier.strip("y"), "unexpected text after captured payload"


def large_output(prefix: str) -> str:
    return prefix + ("x" * LARGE_OUTPUT_SIZE) + ("y" * LARGE_OUTPUT_SIZE)


def remove_length_marker(output: str, marker_prefix: str) -> tuple[str, int]:
    marker_start = output.find(marker_prefix)
    assert marker_start >= 0, (
        f"raw output lost length marker {marker_prefix!r}: {output[-500:]!r}"
    )
    marker_end = output.find("\n", marker_start)
    if marker_end < 0:
        marker_end = len(output)
        after_marker = marker_end
    else:
        after_marker = marker_end + 1
    length = int(output[marker_start + len(marker_prefix) : marker_end])
    return output[:marker_start] + output[after_marker:], length


def test_orders_failure_and_replacement_output(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
        startup_control.write_text("ready with stdout", encoding="utf-8")
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
            "zod replacement startup ready",
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
            "zod replacement startup ready\n"
            "[idle]"
        )

        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"
        return client._finish()


def test_preserves_raw_output_during_forced_stop(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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


def test_reports_missing_worker_launch_failure(binary: Path) -> Transcript:
    client = McpClient(
        binary,
        ("serve", "--worker", "/definitely/missing/mcp-console-worker"),
    )
    client._initialize_and_list_tools()

    client.send(r="complete silently")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    failure = result["content"][0]["text"]
    assert failure.startswith("[failed to launch worker: "), failure
    assert failure.endswith("]"), failure
    result["content"][0]["text"] = "[failed to launch worker: <missing executable>]"

    transcript, standard_error = client._finish_with_standard_error()
    if standard_error:
        assert standard_error.strip() == failure.removeprefix("[").removesuffix("]")
    return transcript


def test_reports_replacement_startup_failure_and_retry(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        startup_control = Path(temporary_directory) / "zod-startup-control"
        startup_control.write_text("ready", encoding="utf-8")
        environment, _ = r_test_environment()
        environment["RETICULATE_PYTHON"] = ""
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        record_resolved_r_library(environment, Path(temporary_directory))
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()

        client.send(r="complete silently")
        assert last_tool_text(client) == "[done]"
        startup_control.write_text("fail with stderr", encoding="utf-8")
        failed = client._start_send(r="exit unexpectedly")
        response_returned = threading.Event()
        forced_stop = threading.Event()

        def stop_if_replacement_loops() -> None:
            if not response_returned.wait(5):
                forced_stop.set()
                stop_process(client.process)

        watchdog = threading.Thread(target=stop_if_replacement_loops, daemon=True)
        watchdog.start()
        try:
            client._receive(failed)
        finally:
            response_returned.set()
            watchdog.join()
        assert not forced_stop.is_set(), "replacement startup retried automatically"
        result = failed["result"]
        assert result == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "[worker sideband read failed: worker sideband closed]\n"
                        "[worker exited with status 86]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "zod replacement startup failed\n"
                        "[worker sideband read failed: worker sideband closed]\n"
                        "[worker exited with status 86]"
                    ),
                }
            ],
            "isError": True,
        }, result

        startup_control.write_text("ready with stdout", encoding="utf-8")
        client.send(
            r="report managed R requirement",
            requirements={"r": ["praise"]},
        )
        assert last_tool_text(client) == (
            "[starting new worker]\nzod replacement startup ready\n"
            "zod R requirement: prepared=true\n"
        )
        return client._finish()


def test_polls_replacement_startup_after_send_timeout(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_release = temporary_path / "zod-startup-release"
        startup_control.write_text("ready", encoding="utf-8")
        environment, _ = r_test_environment()
        environment["RETICULATE_PYTHON"] = ""
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["ZOD_STARTUP_RELEASE"] = str(startup_release)
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        forced_release = threading.Event()
        response_returned = threading.Event()
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="complete silently")
            assert last_tool_text(client) == "[done]"
            startup_control.write_text(
                "block then ready with stdout",
                encoding="utf-8",
            )

            failed = client._start_send(r="exit unexpectedly", timeout_ms=1_000)
            wait_for_marker(
                temporary_path,
                "zod-replacement-waiting-ready",
                client,
            )

            def release_if_send_ignores_timeout() -> None:
                if not response_returned.wait(5):
                    forced_release.set()
                    startup_release.touch()

            watchdog = threading.Thread(
                target=release_if_send_ignores_timeout,
                daemon=True,
            )
            watchdog.start()
            try:
                client._receive(failed)
            finally:
                response_returned.set()
                watchdog.join()
            assert not forced_release.is_set(), "send did not honor its startup timeout"
            assert failed["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[worker sideband read failed: worker sideband closed]\n"
                            "[worker exited with status 86]\n"
                            "[worker stopped: in-memory state lost]\n"
                            "[starting new worker]\n"
                            "[worker starting]"
                        ),
                    }
                ],
                "isError": True,
            }, failed

            client.session(
                action="prepare",
                requirements={"python": ["py-yaml12"]},
            )
            assert client.transcript[-1]["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": "[requirements not prepared: worker is starting]",
                    }
                ],
                "isError": True,
            }, client.transcript[-1]

            combined = client.send(
                r="echo startup overlap cell ran",
                requirements={"r": ["praise"]},
            )
            assert combined == {
                "content": [
                    {
                        "type": "text",
                        "text": "[requirements not prepared: worker is starting]",
                    }
                ],
                "isError": True,
            }, combined
            assert not (temporary_path / "resolved-r-library").exists()

            startup_release.touch()
            client.send(timeout_ms=3_000)
            assert last_tool_text(client) == ("zod replacement startup ready\n[idle]")
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            startup_release.touch()
            if not passed:
                stop_process(client.process)


def test_orders_explicit_restart_output(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
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

        client.send(r="wait for stdin close", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        wait_for_marker(
            temporary_path,
            "zod-waiting-for-stdin-close",
            client,
        )

        startup_control.write_text("ready", encoding="utf-8")
        client.session(action="restart")
        result = client.transcript[-1]["result"]
        assert result["isError"] is False, result
        expected = large_output("zod stdin closed\n") + (
            "\n[active evaluation stopped by session restart request]"
            "\n[worker stopped: in-memory state lost]"
            "\n[starting new worker]"
            "\n[idle]"
        )
        assert result["content"] == [{"type": "text", "text": expected}], result
        result["content"][0]["text"] = (
            "zod stdin closed\n<large output>\n"
            "[active evaluation stopped by session restart request]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )

        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"
        return client._finish()


def test_controlled_restart_runs_cell_once_in_fresh_worker(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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

        client.send(r="set controlled restart state")
        assert last_tool_text(client) == "zod controlled state: old\n"
        old_worker = wait_for_marker(
            temporary_path,
            "zod-controlled-restart-old-worker",
            client,
        )
        old_pid = int(old_worker.read_text(encoding="utf-8"))

        client.send(
            control="restart",
            r="inspect controlled restart state",
        )
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "zod controlled state: fresh; evaluation=1\n"
            "[done]"
        )

        evaluations = wait_for_marker(
            temporary_path,
            "zod-controlled-restart-cell-evaluations",
            client,
        )
        records = evaluations.read_text(encoding="utf-8").splitlines()
        assert len(records) == 1, records
        new_pid, state, count = records[0].split()
        assert int(new_pid) != old_pid, records
        assert (state, count) == ("fresh", "1"), records
        assert not process_exists(old_pid), old_pid

        client.send()
        assert last_tool_text(client) == "\n[idle]"
        assert evaluations.read_text(encoding="utf-8").splitlines() == records
        return client._finish()


def test_controlled_interrupt_preserves_idle_worker_startup_failure(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    ordered_ir = (
        Path(__file__).resolve().parents[2] / "fixtures" / "ordered_retirement_ir"
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_control.write_text("fail with stderr", encoding="utf-8")
        library = temporary_path / "resolved-library"
        library.mkdir()
        fake_bin = temporary_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "ir").symlink_to(ordered_ir)
        resolver_started = FifoCheckpoint(temporary_path / "resolver-started")
        resolver_release = FifoCheckpoint(temporary_path / "resolver-release")
        resolver_interrupted = FifoCheckpoint(temporary_path / "resolver-interrupted")

        environment, _ = r_test_environment()
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["MCP_CONSOLE_TEST_IR_COUNTER"] = str(temporary_path / "ir-counter")
        environment["MCP_CONSOLE_TEST_IR_LIBRARIES"] = str(library)
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        environment["MCP_CONSOLE_TEST_IR_INTERRUPTED"] = str(resolver_interrupted.path)

        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        finished = False
        try:
            client._initialize_and_list_tools()
            preparation = client._start_session(
                action="prepare",
                requirements={"r": ["blocked-resolver"]},
            )
            resolver_started.wait("controlled interrupt R resolver")

            controlled = client._start_send(
                control="interrupt",
                stdin="unused input\n",
            )
            resolver_interrupted.wait("controlled interrupt signal delivery")
            client._receive_many([preparation, controlled])

            assert preparation["result"].get("isError") is True, preparation
            result = controlled["result"]
            assert result == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "zod replacement startup failed\n"
                            "[worker sideband read failed: worker sideband closed]\n"
                            "[worker exited with status 86]"
                        ),
                    }
                ],
                "isError": True,
            }, result

            startup_control.write_text("ready", encoding="utf-8")
            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            finished = True
            return transcript
        finally:
            resolver_release.release()
            resolver_started.close()
            resolver_release.close()
            resolver_interrupted.close()
            if not finished:
                stop_client(client)


def test_cancelled_controlled_restart_response_is_reclaimed_once(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        finished = False
        try:
            client._initialize_and_list_tools()

            client.send(r="wait for stdin close", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            wait_for_marker(
                temporary_path,
                "zod-waiting-for-stdin-close",
                client,
            )

            combined = client._start_send(
                control="restart",
                r="inspect controlled restart state",
            )
            # The response is larger than the stdout pipe. Once its first bytes
            # are readable, cancellation can overtake the blocked write.
            readable, _, _ = select.select([client.stdout], [], [], 10)
            assert readable, "controlled restart did not reach MCP output"
            client._notify(
                "notifications/cancelled",
                requestId=combined["id"],
                reason="acceptance test cancelled the controlled send",
            )
            cancellation = client.transcript[-1]["input"]["params"]
            assert cancellation["requestId"] == combined["id"], cancellation
            cancellation["requestId"] = "<request ID>"

            # Flushing a second oversized message proves the ordered input
            # transport has observed cancellation before the reclaiming poll.
            barrier_size = 2 * LARGE_OUTPUT_SIZE
            client._notify(
                "notifications/acceptance-test-barrier",
                padding="b" * barrier_size,
            )
            barrier = client.transcript[-1]["input"]["params"]
            assert len(barrier["padding"]) == barrier_size, barrier
            barrier["padding"] = f"<input barrier: {barrier_size} bytes>"
            reclaimed = client._start_send()

            discarded = json.loads(client.stdout.readline())
            assert discarded.pop("jsonrpc", None) == "2.0", discarded
            assert discarded.pop("id", None) == combined["id"], discarded
            assert discarded["result"]["isError"] is False, discarded
            assert combined.keys() == {"id", "send"}, combined

            client._receive(reclaimed)
            result = reclaimed["result"]
            expected = large_output("zod stdin closed\n") + (
                "\n[active evaluation stopped by session restart request]"
                "\n[worker stopped: in-memory state lost]"
                "\n[starting new worker]"
                "\nzod controlled state: fresh; evaluation=1\n"
                "[done]"
            )
            assert result == {
                "content": [{"type": "text", "text": expected}],
                "isError": False,
            }, result
            result["content"][0]["text"] = (
                "zod stdin closed\n<large output>\n"
                "[active evaluation stopped by session restart request]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "zod controlled state: fresh; evaluation=1\n"
                "[done]"
            )

            evaluations = wait_for_marker(
                temporary_path,
                "zod-controlled-restart-cell-evaluations",
                client,
            )
            records = evaluations.read_text(encoding="utf-8").splitlines()
            assert len(records) == 1, records
            _, state, count = records[0].split()
            assert (state, count) == ("fresh", "1"), records

            client.send()
            assert last_tool_text(client) == "\n[idle]"
            assert evaluations.read_text(encoding="utf-8").splitlines() == records
            transcript = client._finish()
            finished = True
            return transcript
        finally:
            if not finished:
                stop_client(client)


def test_restart_preserves_pending_sideband_output(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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

        client.send(r="emit output and image before completion", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        image_started = wait_for_marker(
            temporary_path,
            "zod-image-evaluation-started",
            client,
        )
        (image_started.parent / "zod-release-image").touch()
        wait_for_marker(temporary_path, "zod-image-processed", client)

        client.session(action="restart")
        result = client.transcript[-1]["result"]
        assert result == {
            "content": [
                {"type": "text", "text": "before pending image\n"},
                {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                {
                    "type": "text",
                    "text": (
                        "after pending image\n"
                        "[active evaluation stopped by session restart request]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                },
            ],
            "isError": False,
        }, result
        return client._finish()


def test_restart_preserves_completion_boundary_before_idle_output(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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

        client.send(r="start background sideband", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        started = wait_for_marker(
            temporary_path,
            "zod-background-sideband-started",
            client,
        )
        (started.parent / "zod-release-background-sideband").touch()
        wait_for_marker(
            temporary_path,
            "zod-background-sideband-emitted",
            client,
        )

        client.session(action="restart")
        assert last_tool_text(client) == (
            "[done]\n"
            "zod background sideband\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )
        return client._finish()


def test_restart_interrupts_waiting_send(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
        expose_idle_sideband_output(client, temporary_path)

        waiting = client._start_send(
            r="emit output and image before completion",
            timeout_ms=30_000,
        )
        image_started = wait_for_marker(
            temporary_path,
            "zod-image-evaluation-started",
            client,
        )
        (image_started.parent / "zod-release-image").touch()
        wait_for_marker(temporary_path, "zod-image-processed", client)

        restarted = client._start_session(action="restart")
        responses_returned = threading.Event()
        forced_stop = threading.Event()

        def stop_if_calls_block() -> None:
            if not responses_returned.wait(5):
                forced_stop.set()
                stop_process(client.process)

        watchdog = threading.Thread(target=stop_if_calls_block, daemon=True)
        watchdog.start()
        try:
            client._receive(waiting)
            client._receive(restarted)
        finally:
            responses_returned.set()
            watchdog.join()
        assert not forced_stop.is_set(), "restart did not release the waiting send"

        assert restarted["result"] == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "[active evaluation stopped by session restart request]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                },
            ],
            "isError": False,
        }, restarted
        assert waiting["result"] == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "zod background sideband\n"
                        "[output produced while idle]\n"
                        "before pending image\n"
                    ),
                },
                {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                {
                    "type": "text",
                    "text": (
                        "after pending image\n"
                        "[stopped by session restart request before evaluation finished]\n"
                        "[worker stopped: in-memory state lost]"
                    ),
                },
            ],
            "isError": True,
        }, waiting
        return client._finish()


def test_restarts_after_unexpected_sideband_message(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        killpg_marker = temporary_path / "killpg-denied"
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_KILLPG_MARKER"] = str(killpg_marker)
        # The interposer removes its loader variable after reaching the server,
        # so sandbox-exec and Zod do not inherit it.
        environment["DYLD_INSERT_LIBRARIES"] = str(
            build_killpg_denial_interposer(temporary_path)
        )
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="report process group")
            process_group_output = last_tool_text(client)
            process_group_prefix = "zod process group: "
            assert process_group_output.startswith(process_group_prefix), (
                process_group_output
            )
            worker_group = int(
                process_group_output.removeprefix(process_group_prefix).removesuffix(
                    "\n"
                )
            )
            assert process_group_output == f"{process_group_prefix}{worker_group}\n"
            assert worker_group != os.getpgrp(), (
                "Zod did not enter a dedicated process group"
            )
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "zod process group: <process group>\n"
            )
            failed_call = client._start_send(r="violate protocol")
            client._receive(failed_call)
            assert killpg_marker.is_file(), "killpg denial interposer did not run"
            assert int(killpg_marker.read_text(encoding="utf-8")) == worker_group, (
                "killpg denial targeted a different process group"
            )
            result = failed_call["result"]
            assert result["isError"] is True
            actual = result["content"][0]["text"]
            assert actual == (
                "zod output before protocol failure\n"
                "[worker sent an unexpected ready message]\n"
                "[worker terminated by signal 9]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            ), repr(actual)
            restarted_call = client._start_send(r="complete silently")
            client._receive(restarted_call)
            assert last_tool_text(client) == "[done]"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_restarts_after_worker_exit(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(r="exit unexpectedly")
    assert client.transcript[-1]["result"] == {
        "content": [
            {
                "type": "text",
                "text": (
                    "[worker sideband read failed: worker sideband closed]\n"
                    "[worker exited with status 86]\n"
                    "[worker stopped: in-memory state lost]\n"
                    "[starting new worker]\n"
                    "[idle]"
                ),
            }
        ],
        "isError": True,
    }
    client.send(stdin="replacement\n")
    assert last_tool_text(client) == "\n[idle]"
    client.send(r="input without request")
    assert last_tool_text(client) == "zod stdin: replacement\n"
    return client._finish()


def test_reports_unexpected_worker_exit_zero(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(r="exit zero")
    assert client.transcript[-1]["result"] == {
        "content": [
            {
                "type": "text",
                "text": (
                    "[worker sideband read failed: worker sideband closed]\n"
                    "[worker exited with status 0]\n"
                    "[worker stopped: in-memory state lost]\n"
                    "[starting new worker]\n"
                    "[idle]"
                ),
            }
        ],
        "isError": True,
    }

    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"
    return client._finish()


def test_replaces_worker_after_relay_exit(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        killpg_count = temporary_path / "relay-killpg-count"
        environment = os.environ.copy()
        environment["MCP_CONSOLE_TEST_KILLPG_COUNT_MARKER"] = str(killpg_count)
        environment["DYLD_INSERT_LIBRARIES"] = str(
            build_killpg_denial_interposer(temporary_path)
        )
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_pid = None
        relay_pid = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="kill relay and remain live", timeout_ms=5_000)

            result = client.transcript[-1]["result"]
            assert result["isError"] is True, result
            topology, failure = result["content"][0]["text"].split("\n", 1)
            worker, launcher, relay = topology.split("; ")
            worker_pid = int(worker.removeprefix("zod worker pid: "))
            launcher_pid = int(launcher.removeprefix("launcher pid: "))
            relay_pid = int(relay.removeprefix("relay process group: "))
            assert len({worker_pid, launcher_pid, relay_pid}) == 3, topology
            assert failure == (
                "[worker relay stdout closed before retirement completed]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            ), failure
            result["content"][0]["text"] = (
                "zod worker pid: <worker pid>; "
                "launcher pid: <launcher pid>; "
                "relay process group: <relay process group>\n" + failure
            )
            assert not process_exists(worker_pid), "worker outlived its relay"
            assert not process_exists(relay_pid), "server did not reap the relay"
            assert not process_group_exists(relay_pid), (
                "relay process group outlived the relay"
            )
            count, process_group = map(
                int,
                killpg_count.read_text(encoding="utf-8").split(),
            )
            assert count == 1, "server tried to stop the retired relay group twice"
            assert process_group == relay_pid, (
                "server stopped a different process group"
            )

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(relay_pid)
                stop_process_id(worker_pid)
                stop_process(client.process)


def test_restart_closes_worker_stdin(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
        client.send(r="wait for stdin close", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        wait_for_marker(
            temporary_path,
            "zod-waiting-for-stdin-close",
            client,
        )

        client.session(action="restart")
        output = last_tool_text(client)
        prefix = "zod stdin closed\n" + ("x" * LARGE_OUTPUT_SIZE)
        suffix = "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        suffix = "[active evaluation stopped by session restart request]\n" + suffix
        assert output.startswith(prefix), "worker stdin did not close before restart"
        assert output.endswith(suffix), "lifecycle notices followed old-worker output"
        barrier = output.removeprefix(prefix).removesuffix(suffix)
        assert barrier and not barrier.strip("y\n"), "unexpected old-worker output"
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "zod stdin closed\n<large output>\n"
            "[active evaluation stopped by session restart request]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )

        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"
        return client._finish()


def test_restart_force_stops_stalled_worker(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
            client._initialize_and_list_tools()
            client.send(r="stall", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            group_marker = wait_for_marker(
                temporary_path,
                "zod-process-group",
                client,
            )
            worker_group = read_worker_group(group_marker)
            wait_for_marker(temporary_path, "zod-stalled", client)

            restart_call = client._start_session(action="restart")
            wait_for_process_group_exit(worker_group, client)
            client._receive(restart_call)
            assert last_tool_text(client) == (
                "[active evaluation stopped by session restart request]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            )

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_restart_allows_accepted_relay_shutdown_to_finish(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        helper_pid = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="stall accepted relay shutdown", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            helper_marker = wait_for_marker(
                temporary_path,
                "zod-relay-resume-helper",
                client,
            )
            helper_pid = int(helper_marker.read_text(encoding="utf-8"))

            restarted = client._start_session(action="restart")
            wait_for_marker(
                temporary_path,
                "zod-relay-stopped-after-shutdown",
                client,
            )
            client._receive(restarted)
            restart_output = last_tool_text(client)
            assert restart_output == (
                "zod output during relay retirement\n"
                "[active evaluation stopped by session restart request]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            ), restart_output

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            stop_process_id(helper_pid)
            if not passed:
                stop_process(client.process)


def test_restart_outer_force_stops_unresponsive_relay(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        killpg_marker = temporary_path / "killpg-denied"
        late_member_marker = temporary_path / "late-process-group-member"
        late_member_reap_marker = temporary_path / "late-process-group-member-reaped"
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        environment["MCP_CONSOLE_TEST_KILLPG_MARKER"] = str(killpg_marker)
        environment["MCP_CONSOLE_TEST_LATE_MEMBER_MARKER"] = str(late_member_marker)
        environment["MCP_CONSOLE_TEST_LATE_MEMBER_REAP_MARKER"] = str(
            late_member_reap_marker
        )
        environment["DYLD_INSERT_LIBRARIES"] = str(
            build_killpg_denial_interposer(temporary_path)
        )
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        helper_pid = None
        worker_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="stall with stopped relay", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            helper_marker = wait_for_marker(
                temporary_path,
                "zod-relay-stop-helper",
                client,
            )
            helper_pid = int(helper_marker.read_text(encoding="utf-8"))
            wait_for_marker(temporary_path, "zod-relay-stopped", client)
            relay_target, launcher_pid = map(
                int,
                wait_for_marker(
                    temporary_path,
                    "zod-relay-stop-target",
                    client,
                )
                .read_text(encoding="utf-8")
                .split(),
            )
            worker_group = read_worker_group(
                wait_for_marker(temporary_path, "zod-process-group", client)
            )
            assert relay_target == worker_group, (
                "helper did not stop the sandbox process-group leader"
            )
            assert launcher_pid != relay_target, (
                "Zod launcher unexpectedly identified the relay"
            )
            assert os.getpgid(launcher_pid) == relay_target, (
                "Zod launcher did not inherit the relay process group"
            )

            restarted = client._start_session(action="restart")
            received = threading.Event()
            errors: list[BaseException] = []

            def receive_restart() -> None:
                try:
                    client._receive(restarted)
                except BaseException as error:
                    errors.append(error)
                finally:
                    received.set()

            restart_started = time.monotonic()
            receiver = threading.Thread(target=receive_restart, daemon=True)
            receiver.start()
            assert received.wait(2), "restart outlived its original shutdown deadline"
            restart_elapsed = time.monotonic() - restart_started
            receiver.join()
            if errors:
                raise errors[0]

            assert restart_elapsed < 2, f"restart took {restart_elapsed:.3f} seconds"
            assert int(killpg_marker.read_text(encoding="utf-8")) == worker_group
            late_member, late_member_group = map(
                int,
                late_member_marker.read_text(encoding="utf-8").split(),
            )
            assert late_member > 0, "invalid late process-group member PID"
            assert late_member_group == worker_group, (
                "late member joined a different process group"
            )
            assert int(late_member_reap_marker.read_text(encoding="utf-8")) == (
                late_member
            ), "a different late process-group member was reaped"
            assert not process_group_exists(worker_group), (
                "stopped relay process group outlived restart"
            )
            assert not process_exists(relay_target), "server did not reap the relay"
            assert last_tool_text(client) == (
                "[active evaluation stopped by session restart request]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            )

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            stop_process_id(helper_pid)
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_restart_starts_first_worker_and_waits_until_ready(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
            client._initialize_and_list_tools()
            restarted = client._start_session(action="restart")
            wait_for_marker(
                temporary_path,
                "zod-replacement-waiting-ready",
                client,
            )
            worker_group = read_worker_group(
                wait_for_marker(temporary_path, "zod-process-group", client)
            )

            while_restarting = client._start_send(r="echo echo")
            client._receive(while_restarting)
            result = while_restarting["result"]
            assert result["isError"] is True
            assert result["content"][0]["text"] == "[worker is restarting]"

            startup_release.touch()
            client._receive(restarted)
            assert restarted["result"]["content"][0]["text"] == (
                "[starting new worker]\n[idle]"
            )

            after_restart = client._start_send(r="echo echo")
            client._receive(after_restart)
            assert after_restart["result"]["content"][0]["text"] == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_restart_does_not_report_never_ready_worker_as_stopped(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_release = temporary_path / "zod-startup-release"
        startup_control.write_text(
            "block with detached sideband writer",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["ZOD_STARTUP_RELEASE"] = str(startup_release)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        descendant_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            waiting = client._start_send(r="echo echo", timeout_ms=30_000)
            wait_for_marker(
                temporary_path,
                "zod-replacement-waiting-ready",
                client,
            )
            marker = wait_for_marker(
                temporary_path,
                "zod-detached-startup-sideband-pid",
                client,
            )
            descendant_group = int(marker.read_text(encoding="utf-8"))

            startup_control.write_text("ready", encoding="utf-8")
            restarted = client._start_session(action="restart")
            responses_returned = threading.Event()
            forced_stop = threading.Event()

            def stop_if_calls_block() -> None:
                if not responses_returned.wait(5):
                    forced_stop.set()
                    stop_process(client.process)

            watchdog = threading.Thread(target=stop_if_calls_block, daemon=True)
            watchdog.start()
            try:
                client._receive(waiting)
                client._receive(restarted)
            finally:
                responses_returned.set()
                watchdog.join()
            assert not forced_stop.is_set(), "restart did not finish initial startup"

            assert waiting["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[stopped by session restart request before "
                            "evaluation finished]"
                        ),
                    }
                ],
                "isError": True,
            }, waiting
            assert restarted["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[active evaluation stopped by session restart request]\n"
                            "[starting new worker]\n"
                            "[idle]"
                        ),
                    }
                ],
                "isError": False,
            }, restarted

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            startup_release.touch()
            stop_process_group(descendant_group)
            if not passed:
                stop_process(client.process)


def test_restart_commits_lifecycle_before_replacement_callbacks(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
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

        startup_control.write_text("ready with callback", encoding="utf-8")
        client.session(action="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        callback = wait_for_marker(
            temporary_path,
            "zod-startup-callback-response",
            client,
        )
        assert callback.read_text(encoding="utf-8") == (
            "Python requirements are unavailable with a custom worker"
        )
        callback.unlink()

        client.session(action="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        callback = wait_for_marker(
            temporary_path,
            "zod-startup-callback-response",
            client,
        )
        assert callback.read_text(encoding="utf-8") == (
            "Python requirements are unavailable with a custom worker"
        )

        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"
        return client._finish()


def test_restart_discards_unread_stdin(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(stdin="stale\n")
    assert last_tool_text(client) == "\n[idle]"

    client.session(action="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )

    client.send(r="input without request", stdin="fresh\n")
    assert last_tool_text(client) == "zod stdin: fresh\n"
    return client._finish()


def test_retries_initial_startup_silently(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
        client._initialize_and_list_tools()
        client.send(r="echo echo")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "[worker sideband read failed: worker sideband closed]\n"
            "[worker exited with status 86]"
        )
        startup_control.write_text("ready", encoding="utf-8")
        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"
        return client._finish()


def test_runs_worker_inside_sandbox(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
        client._initialize_and_list_tools()
        client.send(r="probe sandbox")
        transcript = client._finish()

        assert host_file.read_text(encoding="utf-8") == "host data"
        return transcript


def test_shuts_down_stalled_worker(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
            client._initialize_and_list_tools()
            stalled = client._start_send(
                r="stall",
                stdin="x" * (2 * 1024 * 1024),
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


def expose_idle_sideband_output(
    client: McpClient,
    temporary_path: Path,
    marker: str | None = None,
) -> None:
    suffix = f"-{marker}" if marker else ""
    source = (
        f"start background sideband: {marker}"
        if marker
        else "start background sideband"
    )
    client.send(r=source)
    assert last_tool_text(client) == "[done]", repr(last_tool_text(client))
    started = wait_for_marker(
        temporary_path,
        f"zod-background-sideband-started{suffix}",
        client,
    )
    (started.parent / f"zod-release-background-sideband{suffix}").touch()
    wait_for_marker(
        temporary_path,
        f"zod-background-sideband-emitted{suffix}",
        client,
    )


def test_demarcates_idle_prelude_across_cell_outcomes(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        passed = False
        try:
            client._initialize_and_list_tools()

            expose_idle_sideband_output(client, temporary_path, "success")
            client.send(r="echo echo")
            assert last_tool_text(client) == (
                "zod background sideband\n[output produced while idle]\nzod: echo\n"
            )

            expose_idle_sideband_output(client, temporary_path, "timeout")
            timed_out = client._start_send(
                r="output then complete after release",
                timeout_ms=1_000,
            )
            processed = wait_for_marker(
                temporary_path,
                "zod-cell-output-processed",
                client,
            )
            client._receive(timed_out)
            assert timed_out["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "zod background sideband\n"
                            "[output produced while idle]\n"
                            "zod cell output before completion\n\n"
                            "[running; poll with an empty send]"
                        ),
                    }
                ],
                "isError": False,
            }, timed_out

            (processed.parent / "zod-release-evaluation").touch()
            client.send(timeout_ms=3_000)
            assert last_tool_text(client) == (
                "zod: output then complete after release\n"
            ), repr(last_tool_text(client))

            expose_idle_sideband_output(client, temporary_path, "input")
            client.send(r="request input", timeout_ms=3_000)
            assert last_tool_text(client) == (
                "zod background sideband\n"
                "[output produced while idle]\n"
                '[input requested: "zod> "]\n'
                "[waiting for stdin]"
            )
            client.send(stdin="answer\n", timeout_ms=3_000)
            assert last_tool_text(client) == "zod stdin: answer\n"

            expose_idle_sideband_output(client, temporary_path, "language-error")
            client.send(r="language error")
            assert last_tool_text(client) == (
                "zod background sideband\n"
                "[output produced while idle]\n"
                "zod language error\n"
            )

            expose_idle_sideband_output(client, temporary_path, "replacement")
            client.send(r="exit unexpectedly")
            result = client.transcript[-1]["result"]
            assert result["isError"] is True, result
            assert result["content"] == [
                {
                    "type": "text",
                    "text": (
                        "zod background sideband\n"
                        "[output produced while idle]\n"
                        "[worker sideband read failed: worker sideband closed]\n"
                        "[worker exited with status 86]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                }
            ], result
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process(client.process)


def test_restart_cancels_partial_sideband_frame(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        descendant_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="start partial sideband descendant", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            marker = wait_for_marker(
                temporary_path,
                "zod-sideband-descendant-pid",
                client,
            )
            descendant_group = int(marker.read_text(encoding="utf-8"))
            release_partial_sideband(marker)

            restarted = client._start_session(action="restart")
            received = threading.Event()
            errors: list[BaseException] = []

            def receive_restart() -> None:
                try:
                    client._receive(restarted)
                except BaseException as error:
                    errors.append(error)
                finally:
                    received.set()

            receiver = threading.Thread(target=receive_restart, daemon=True)
            receiver.start()
            assert received.wait(3), "restart waited for a partial sideband frame"
            receiver.join()
            if errors:
                raise errors[0]
            assert last_tool_text(client) == (
                "[active evaluation stopped by session restart request]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            )

            stop_process_group(descendant_group)
            descendant_group = None
            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            stop_process_group(descendant_group)
            if not passed:
                stop_process(client.process)


def test_restart_cancels_reader_after_operation_result(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        descendant_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="complete before partial sideband descendant")
            assert last_tool_text(client) == "[done]"
            marker = wait_for_marker(
                temporary_path,
                "zod-sideband-descendant-pid",
                client,
            )
            descendant_group = int(marker.read_text(encoding="utf-8"))

            restarted = client._start_session(action="restart")
            received = threading.Event()
            errors: list[BaseException] = []

            def receive_restart() -> None:
                try:
                    client._receive(restarted)
                except BaseException as error:
                    errors.append(error)
                finally:
                    received.set()

            receiver = threading.Thread(target=receive_restart, daemon=True)
            receiver.start()
            assert received.wait(3), "restart waited for the sideband reader"
            receiver.join()
            if errors:
                raise errors[0]
            assert last_tool_text(client) == (
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            )

            stop_process_group(descendant_group)
            descendant_group = None
            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            stop_process_group(descendant_group)
            if not passed:
                stop_process(client.process)


def test_shutdown_cancels_partial_sideband_frame(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        descendant_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="start partial sideband descendant", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            marker = wait_for_marker(
                temporary_path,
                "zod-sideband-descendant-pid",
                client,
            )
            descendant_group = int(marker.read_text(encoding="utf-8"))
            release_partial_sideband(marker)

            shutdown_started = time.monotonic()
            client.stdin.close()
            try:
                return_code = client.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    "mcp-console waited for a partial sideband frame"
                ) from None
            shutdown_elapsed = time.monotonic() - shutdown_started

            assert shutdown_elapsed < 1.5, (
                f"worker shutdown took {shutdown_elapsed:.3f} seconds"
            )
            assert return_code == 0, client.stderr.read()
            client.stdout.read()
            assert client.stderr.read() == ""
            stop_process_group(descendant_group)
            descendant_group = None
            passed = True
            return client.transcript
        finally:
            stop_process_group(descendant_group)
            if not passed:
                stop_process(client.process)


def test_shutdown_deadline_does_not_wait_for_sideband_writer(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
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
            client._initialize_and_list_tools()
            entry = client._start_send(r="x" * (2 * 1024 * 1024))
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


def wait_for_stopped_worker(
    root: Path,
    previous_process_ids: set[int],
    recorded_workers: list[tuple[int, int]],
    client: McpClient,
) -> tuple[Path, int, int]:
    deadline = time.monotonic() + 3
    while True:
        for marker in root.glob("**/zod-stop-continue-worker"):
            process_id, parent_id, process_group = map(
                int,
                marker.read_text(encoding="utf-8").split(),
            )
            if process_id in previous_process_ids:
                continue
            worker = (process_id, process_group)
            if worker not in recorded_workers:
                recorded_workers.append(worker)
            assert parent_id == process_group, (
                "stopped worker is not the relay's direct child"
            )
            assert process_id != process_group, (
                "stopped worker unexpectedly leads the relay process group"
            )
            assert process_group != os.getpgrp(), (
                "stopped worker shares the test process group"
            )
            status = read_process_status(process_id)
            if status is not None and status[2].startswith("T"):
                assert status[:2] == (parent_id, process_group), (
                    "stopped worker changed its process boundary"
                )
                return marker, process_id, process_group
        assert client.process.poll() is None, (
            "mcp-console stopped before its direct worker reached SIGSTOP"
        )
        assert time.monotonic() < deadline, (
            "direct worker did not enter the stopped process state"
        )
        time.sleep(0.01)


def wait_for_path(path: Path, description: str, client: McpClient) -> None:
    deadline = time.monotonic() + 3
    while not path.exists():
        assert client.process.poll() is None, (
            f"mcp-console stopped before {description}"
        )
        assert time.monotonic() < deadline, f"timed out waiting for {description}"
        time.sleep(0.01)


def read_process_status(process_id: int) -> tuple[int, int, str] | None:
    status = subprocess.run(
        [
            "ps",
            "-o",
            "ppid=",
            "-o",
            "pgid=",
            "-o",
            "state=",
            "-p",
            str(process_id),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if status.returncode == 1 and not status.stdout.strip():
        return None
    assert status.returncode == 0, status.stderr
    fields = status.stdout.split()
    assert len(fields) == 3, status.stdout
    return int(fields[0]), int(fields[1]), fields[2]


def continue_stopped_worker(process_id: int, process_group: int) -> None:
    status = read_process_status(process_id)
    assert status is not None, "stopped worker exited before SIGCONT"
    assert status[1] == process_group, "stopped worker changed process groups"
    assert status[2].startswith("T"), "worker was not stopped before SIGCONT"
    os.kill(process_id, signal.SIGCONT)


def wait_for_worker_retirement(
    process_id: int,
    process_group: int,
    client: McpClient,
) -> None:
    deadline = time.monotonic() + 3
    while read_process_status(process_id) is not None or process_group_exists(
        process_group
    ):
        assert client.process.poll() is None, (
            "mcp-console stopped while retiring the old worker generation"
        )
        assert time.monotonic() < deadline, (
            "restart did not retire the old worker and relay process group"
        )
        time.sleep(0.01)


def stop_recorded_worker(process_id: int, process_group: int) -> None:
    assert process_group != os.getpgrp(), "refusing to stop the test process group"
    stop_process_group(process_group)
    status = read_process_status(process_id)
    if status is not None and status[1] == process_group:
        stop_process_id(process_id)


def read_worker_group(marker: Path) -> int:
    worker_group = int(marker.read_text(encoding="utf-8"))
    assert worker_group != os.getpgrp(), "Zod did not enter a dedicated process group"
    return worker_group


def release_partial_sideband(marker: Path) -> None:
    release = marker.with_name("zod-release-partial-sideband")
    with release.open("wb", buffering=0) as stream:
        assert stream.write(b"x") == 1


def process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_process_id(process_id: int | None) -> None:
    if process_id is None:
        return
    try:
        os.kill(process_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


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
