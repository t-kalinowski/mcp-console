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
    expose_idle_input_request,
    expose_idle_sideband_output,
    _zod_last_tool_text as last_tool_text,
    record_resolved_r_library,
    wait_for_marker,
)


def test_custom_worker_skips_managed_python_preflight(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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
    result = client.send(requirements={"python": ["py-yaml12"]})
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "Python requirements are unavailable with a custom worker"
    ), result
    result = client.send(
        control="restart",
        requirements={"python": ["py-yaml12"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "Python requirements are unavailable with a custom worker"
    ), result
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


def test_standalone_preparation_before_worker_startup_is_causal_and_idempotent(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    relay = Path(__file__).resolve().parents[3] / "fixtures" / "scripted_relay"
    ir = Path(__file__).resolve().parents[3] / "fixtures" / "ordered_retirement_ir"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        library = temporary / "standalone-candidate"
        library.mkdir()
        fake_bin = temporary / "bin"
        fake_bin.mkdir()
        (fake_bin / "ir").symlink_to(ir)
        resolver_started = FifoCheckpoint(temporary / "resolver-started")
        resolver_release = FifoCheckpoint(temporary / "resolver-release")
        worker_started = temporary / "zod-started"
        resolver_counter = temporary / "ir-counter"
        environment, _ = r_test_environment()
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_IR_COUNTER"] = str(resolver_counter)
        environment["MCP_CONSOLE_TEST_IR_LIBRARIES"] = str(library)
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        environment["MCP_CONSOLE_TEST_RELAY_SCENARIO"] = "ready"
        environment["MCP_CONSOLE_TEST_ZOD_STARTED"] = str(worker_started)
        client = McpClient(
            binary,
            (
                "serve",
                "--worker",
                str(zod),
                "--relay",
                str(relay),
            ),
            environment,
        )
        finished = False
        released = False
        try:
            client._initialize_and_list_tools()
            invalid = client._start_send(
                requirements={"r": ["must-not-resolve"]},
                stdin="must not queue\n",
            )
            readable, _, _ = select.select(
                [client.stdout, resolver_started.descriptor],
                [],
                [],
                10,
            )
            assert client.stdout in readable, (
                "requirements with standalone stdin did not return validation"
            )
            assert resolver_started.descriptor not in readable, (
                "requirements with standalone stdin started a resolver"
            )
            client._receive(invalid)
            assert invalid["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "requirements-only `send` performs standalone "
                            "preparation and cannot also queue stdin"
                        ),
                    }
                ],
                "isError": True,
            }, invalid
            assert not resolver_counter.exists(), resolver_counter
            assert not worker_started.exists(), worker_started
            assert not list(
                temporary.glob("mcp-console-tmp-*/mcp-console-server-relay-wire.jsonl")
            )

            preparation = client._start_send(
                requirements={"r": ["standalone-requirement"]},
                timeout_ms=0,
            )
            resolver_started.wait("standalone requirement resolver")
            assert not worker_started.exists(), worker_started
            assert not list(
                temporary.glob("mcp-console-tmp-*/mcp-console-server-relay-wire.jsonl")
            )
            readable, _, _ = select.select([client.stdout], [], [], 0.25)
            assert not readable, "timeout_ms applied to standalone preparation"

            resolver_release.release()
            released = True
            client._receive(preparation)
            assert preparation["result"] == {
                "content": [{"type": "text", "text": "[prepared]"}],
                "isError": False,
            }, preparation
            assert resolver_counter.read_text(encoding="utf-8") == "1"
            assert not worker_started.exists(), worker_started
            assert not list(
                temporary.glob("mcp-console-tmp-*/mcp-console-server-relay-wire.jsonl")
            )

            repeated = client.send(
                requirements={"r": ["standalone-requirement"]},
                timeout_ms=0,
            )
            assert repeated == {
                "content": [{"type": "text", "text": "[prepared]"}],
                "isError": False,
            }, repeated
            assert resolver_counter.read_text(encoding="utf-8") == "1"
            assert not worker_started.exists(), worker_started
            assert not list(
                temporary.glob("mcp-console-tmp-*/mcp-console-server-relay-wire.jsonl")
            )
            transcript = client._finish()
            finished = True
            return transcript
        finally:
            if not released:
                resolver_release.release()
            resolver_started.close()
            resolver_release.close()
            if not finished:
                stop_client(client)


def test_custom_worker_starts_without_home(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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

        client.send(requirements={"r": ["praise"]})
        assert last_tool_text(client) == "[prepared]"

        client.send(requirements={"duckdb": ["json"]})
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


def test_custom_worker_reports_idle_input_before_preparation_failure(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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
        expose_idle_input_request(client, temporary_path)

        result = client.send(requirements={"r": ["praise"]})
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            '[idle R callback requested input "idle> " during requirement '
            "preparation; collect callback input with send before preparing requirements]\n"
            "[worker terminated by signal 9]\n"
            "[worker stopped: in-memory state lost]"
        ), result
        result = client.send(requirements={"r": ["zeallot"]})
        assert result == {
            "content": [{"type": "text", "text": "[restart required]"}],
            "isError": False,
        }, result
        return client._finish()


def test_custom_worker_resolves_idle_activity_before_preparation(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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

        client.send(requirements={"r": ["praise"]})
        assert last_tool_text(client) == "[prepared]"
        client.send(r="report managed R requirement")
        assert last_tool_text(client) == "zod R requirement: prepared=true\n"
        return client._finish()


def test_combined_requirements_keep_idle_output_as_one_prelude(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        environment["TMPDIR"] = temporary
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

        expose_idle_input_request(client, temporary_path)
        poll_start = len(client.transcript)
        submitted = client._start_send(r="echo echo", stdin="continue\n")
        wait_for_marker(
            temporary_path,
            "zod-idle-input-received",
            client,
        )
        client._receive(submitted)
        output = last_tool_text(client)
        if output != "zod: echo\n":
            assert output == "\n[waiting for stdin]", repr(output)
            client.send()
            output = last_tool_text(client)
        assert output == "zod: echo\n", repr(output)
        calls = client.transcript[poll_start:]
        submitted["result"] = calls[-1]["result"]
        client.transcript[poll_start:] = [submitted]
        return client._finish()


def test_custom_worker_restart_prepares_r_and_duckdb_requirements(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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

        client.send(
            control="restart",
            requirements={"r": ["praise"], "duckdb": ["json"]},
        )
        assert last_tool_text(client) == "[starting new worker]\n[idle]"

        client.send(r="report managed R requirement")
        assert last_tool_text(client) == "zod R requirement: prepared=true\n"
        return client._finish()


if __name__ == "__main__":
    run_this_suite(__file__)
