#!/usr/bin/env -S uv run --script

import os
import select
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import (
    FifoCheckpoint,
    McpClient,
    Transcript,
    r_test_environment,
    run_this_suite,
    stop_client,
)

PLATFORMS = {"darwin"}
FIXTURE_CHECKPOINT_TIMEOUT_SECONDS = 15
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)

from client_server._harness import (
    expose_idle_sideband_output,
    large_output,
    _zod_last_tool_text as last_tool_text,
    process_exists,
    process_group_exists,
    record_resolved_r_library,
    release_fixture_checkpoint,
    stop_process,
    stop_process_group,
    stop_process_id,
    wait_for_marker,
)


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
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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
        wait_for_marker(
            Path(temporary_directory),
            "zod-replacement-startup-failing",
            client,
        )
        response_returned = threading.Event()
        forced_stop = threading.Event()

        def stop_if_replacement_loops() -> None:
            if not response_returned.wait(FIXTURE_CHECKPOINT_TIMEOUT_SECONDS):
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

        startup_control.write_text("ready", encoding="utf-8")
        client.send(
            r="report managed R requirement",
            requirements={"r": ["praise"]},
        )
        assert last_tool_text(client) == (
            "[starting new worker]\nzod R requirement: prepared=true\n"
        )
        return client._finish()


def test_polls_replacement_startup_after_send_timeout(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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
                "block",
                encoding="utf-8",
            )

            failed = client._start_send(r="exit unexpectedly", timeout_ms=1_000)
            wait_for_marker(
                temporary_path,
                "zod-replacement-waiting-ready",
                client,
            )

            def release_if_send_ignores_timeout() -> None:
                if not response_returned.wait(FIXTURE_CHECKPOINT_TIMEOUT_SECONDS):
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

            client.send(requirements={"python": ["py-yaml12"]})
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
            client.send()
            assert last_tool_text(client) == "[idle]"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            startup_release.touch()
            if not passed:
                stop_process(client.process)


def test_orders_explicit_restart_output(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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
        client.send(control="restart")
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
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    ordered_ir = (
        Path(__file__).resolve().parents[3] / "fixtures" / "ordered_retirement_ir"
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
            preparation = client._start_send(
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


def test_control_only_interrupt_returns_while_explicit_preparation_settles(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    ordered_ir = (
        Path(__file__).resolve().parents[3] / "fixtures" / "ordered_retirement_ir"
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        library = temporary_path / "resolved-library"
        library.mkdir()
        fake_bin = temporary_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "ir").symlink_to(ordered_ir)
        resolver_started = FifoCheckpoint(temporary_path / "resolver-started")
        resolver_release = FifoCheckpoint(temporary_path / "resolver-release")
        resolver_interrupted = FifoCheckpoint(temporary_path / "resolver-interrupted")
        interrupt_release = FifoCheckpoint(temporary_path / "interrupt-release")

        environment, _ = r_test_environment()
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_IR_COUNTER"] = str(temporary_path / "ir-counter")
        environment["MCP_CONSOLE_TEST_IR_LIBRARIES"] = str(library)
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        environment["MCP_CONSOLE_TEST_IR_INTERRUPTED"] = str(resolver_interrupted.path)
        environment["MCP_CONSOLE_TEST_IR_INTERRUPT_RELEASE"] = str(
            interrupt_release.path
        )

        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        finished = False
        interrupt_waiting = False
        try:
            client._initialize_and_list_tools()

            def interrupt_preparation(
                preparation_arguments: dict[str, object],
                description: str,
            ) -> None:
                nonlocal interrupt_waiting
                preparation = client._start_send(**preparation_arguments)
                resolver_started.wait(description)

                interrupt = client._start_send(
                    control="interrupt",
                    timeout_ms=0,
                )
                resolver_interrupted.wait(f"{description} interrupt")
                interrupt_waiting = True
                readable, _, _ = select.select([client.stdout], [], [], 3)
                assert client.stdout in readable, (
                    "control-only interrupt waited for explicit preparation to settle"
                )
                client._receive(interrupt)
                assert interrupt["result"] == {
                    "content": [
                        {
                            "type": "text",
                            "text": "\n[running; poll with an empty send]",
                        }
                    ],
                    "isError": False,
                }, interrupt

                interrupt_release.release()
                interrupt_waiting = False
                client._receive(preparation)
                assert preparation["result"] == {
                    "content": [
                        {
                            "type": "text",
                            "text": "R package resolution failed with exit status: 130: ",
                        }
                    ],
                    "isError": True,
                }, preparation

            interrupt_preparation(
                {"requirements": {"r": ["blocked-standalone-resolver"]}},
                "standalone preparation resolver",
            )

            client.send(r="echo worker ready")
            assert last_tool_text(client) == "zod: worker ready\n"

            interrupt_preparation(
                {
                    "r": "echo interrupted preparation cell ran",
                    "requirements": {"r": ["blocked-cell-resolver"]},
                },
                "cell preparation resolver",
            )

            client.send(r="echo worker remains usable")
            assert last_tool_text(client) == "zod: worker remains usable\n"
            transcript = client._finish()
            finished = True
            return transcript
        finally:
            if interrupt_waiting:
                interrupt_release.release()
            resolver_release.release()
            resolver_started.close()
            resolver_release.close()
            resolver_interrupted.close()
            interrupt_release.close()
            if not finished:
                stop_client(client)


def test_restart_preserves_pending_sideband_output(binary: Path) -> Transcript:
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

        client.send(r="emit output and image before completion", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        image_started = wait_for_marker(
            temporary_path,
            "zod-image-evaluation-started",
            client,
        )
        (image_started.parent / "zod-release-image").touch()
        wait_for_marker(temporary_path, "zod-image-processed", client)

        client.send(control="restart")
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


def test_restart_preserves_unpolled_completion(binary: Path) -> Transcript:
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

        client.send(r="complete before restart checkpoint", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        wait_for_marker(
            temporary_path,
            "zod-completion-processed",
            client,
        )

        client.send(control="restart")
        restart_output = last_tool_text(client)
        assert restart_output == (
            "[done]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        ), restart_output
        return client._finish()


def test_restart_interrupts_waiting_send(binary: Path) -> Transcript:
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

        restarted = client._start_send(control="restart")
        responses_returned = threading.Event()
        forced_stop = threading.Event()

        def stop_if_calls_block() -> None:
            if not responses_returned.wait(FIXTURE_CHECKPOINT_TIMEOUT_SECONDS):
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
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
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
            assert not process_exists(worker_group), (
                "server did not reap the failed generation's relay"
            )
            assert not process_group_exists(worker_group), (
                "failed worker generation survived sandbox manager retirement"
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
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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
        worker_pid = None
        launcher_pid = None
        relay_pid = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="kill relay and remain live", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            started = wait_for_marker(
                temporary_path,
                "zod-relay-exit-evaluation-started",
                client,
            )
            release_fixture_checkpoint(started.parent / "zod-release-relay-exit")
            client.send()

            result = client.transcript[-1]["result"]
            assert result["isError"] is True, result
            text = result["content"][0]["text"]
            assert text.startswith("zod worker pid: "), text
            topology, failure = text.split("\n", 1)
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
            assert not process_exists(launcher_pid), (
                "worker launcher outlived its relay"
            )
            assert not process_exists(relay_pid), "server did not reap the relay"
            assert not process_group_exists(relay_pid), (
                "relay process group outlived sandbox manager retirement"
            )

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(relay_pid)
                stop_process_id(launcher_pid)
                stop_process_id(worker_pid)
                stop_process(client.process)


if __name__ == "__main__":
    run_this_suite(__file__)
