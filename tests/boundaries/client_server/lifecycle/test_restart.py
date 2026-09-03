#!/usr/bin/env -S uv run --script

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.processes import (
    process_exists,
    process_group_exists,
    stop_process,
    stop_process_group,
    stop_process_id,
)
from support.records import Transcript
from support.suites import run_this_suite

PLATFORMS = {"darwin"}
LARGE_OUTPUT_SIZE = 2 * 1024 * 1024
PENDING_TEXT_BUDGET = 8 * 1024 * 1024
FIXTURE_CHECKPOINT_TIMEOUT_SECONDS = 15

from boundaries.client_server._harness import (
    ZodFixtureControl,
    read_worker_group,
    wait_for_marker,
    wait_for_process_group_exit,
    wait_for_stopped_process,
)


def test_restart_closes_worker_stdin(binary: Path) -> Transcript:
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
        client.send(r="wait for stdin close", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        wait_for_marker(
            temporary_path,
            "zod-waiting-for-stdin-close",
            client,
        )

        client.send(control="restart")
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
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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

            restart_call = client._start_send(control="restart")
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
            helper_pid, relay_target = map(
                int,
                helper_marker.read_text(encoding="utf-8").split(),
            )

            restarted = client._start_send(control="restart")
            stopped_marker = wait_for_marker(
                temporary_path,
                "zod-relay-stopped-after-shutdown",
                client,
            )
            wait_for_stopped_process(
                relay_target,
                relay_target,
                client,
                "accepted worker relay shutdown",
            )
            wait_for_marker(
                temporary_path,
                "zod-relay-retirement-output-written",
                client,
            )
            with stopped_marker.with_name("zod-accepted-relay-stop-observed").open(
                "wb", buffering=0
            ) as checkpoint:
                assert checkpoint.write(b"1") == 1
            client._receive(restarted)
            assert not process_exists(helper_pid), (
                "detached relay-resume helper outlived sandbox retirement"
            )
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
            if not passed:
                stop_process_id(helper_pid)
                stop_process(client.process)


def test_restart_outer_force_stops_unresponsive_relay(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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
            assert os.getpgid(helper_pid) == helper_pid, (
                "relay-stop helper did not detach from the relay process group"
            )
            wait_for_stopped_process(
                relay_target,
                worker_group,
                client,
                "outer relay force-stop",
            )

            restarted = client._start_send(control="restart")
            retirement_deadline = time.monotonic() + 5
            while (
                process_exists(relay_target)
                or process_group_exists(worker_group)
                or process_exists(helper_pid)
            ):
                assert client.process.poll() is None, (
                    "mcp-console stopped while retiring the sandbox lifetime"
                )
                assert time.monotonic() < retirement_deadline, (
                    "sandbox manager did not retire the stopped relay, its process "
                    "group, and its detached descendant within the deadline"
                )
                time.sleep(0.01)
            client._receive(restarted)
            assert not process_group_exists(worker_group), (
                "stopped relay process group outlived restart"
            )
            assert not process_exists(relay_target), "server did not reap the relay"
            assert not process_exists(helper_pid), (
                "detached worker descendant outlived sandbox retirement"
            )
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
                stop_process_id(helper_pid)
                stop_process_group(worker_group)
                stop_process(client.process)


def test_restart_starts_first_worker_and_waits_until_ready(
    binary: Path,
) -> Transcript:
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
            restarted = client._start_send(control="restart")
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
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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
            assert not process_group_exists(descendant_group), (
                "detached startup descendant outlived sandbox retirement"
            )

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            startup_release.touch()
            if not passed:
                stop_process_group(descendant_group)
                stop_process(client.process)


def test_restart_commits_lifecycle_before_replacement_callbacks(
    binary: Path,
) -> Transcript:
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
        client.send(r="complete silently")
        assert last_tool_text(client) == "[done]"

        startup_control.write_text("ready with callback", encoding="utf-8")
        client.send(control="restart")
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

        client.send(control="restart")
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
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(stdin="stale\n")
    assert last_tool_text(client) == "\n[idle]"

    client.send(control="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )

    client.send(r="input without request", stdin="fresh\n")
    assert last_tool_text(client) == "zod stdin: fresh\n"
    return client._finish()


def test_retries_initial_startup_silently(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
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
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with (
        tempfile.TemporaryDirectory() as temporary_directory,
        ZodFixtureControl(Path(temporary_directory)) as control,
    ):
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        control.configure(environment)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            operation = client._next_request_id
            stalled = client._start_send(
                r=f"stall: {operation}",
                stdin="x" * (2 * 1024 * 1024),
            )
            stalled["send"]["r"] = "stall"
            stalled["send"]["stdin"] = "<large stdin>"
            control.connect(client)
            event = control.wait_for(operation, "parent_operation_stalled")
            worker_group = event["process_group"]
            assert isinstance(worker_group, int) and worker_group > 0, event
            client.stdin.close()
            try:
                return_code = client.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    "mcp-console did not stop its stalled worker; "
                    + control.diagnostics()
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


def test_shutdown_is_bounded_with_detached_stdin_descendant(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment = os.environ.copy()
    with ZodFixtureControl() as control:
        control.configure(environment)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        descendant_group = None
        descendant_retired = False
        worker_group = None
        server_stopped = False
        try:
            client._initialize_and_list_tools()
            client.send(r="echo ready")
            assert last_tool_text(client) == "zod: ready\n"
            control.connect(client)

            operation = client._next_request_id
            client.send(
                r=f"stall with detached stdin: {operation}",
                timeout_ms=0,
            )
            submitted = client.transcript[-1]
            assert submitted["id"] == operation, submitted
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            submitted["send"]["r"] = "<stall with detached stdin>"

            control.wait_for(operation, "worker_operation_started")
            created = control.wait_for(operation, "detached_descendant_created")
            created_group = created["process_group"]
            assert isinstance(created_group, int) and created_group > 0, created
            assert created["pid"] == created_group, created
            assert created_group != os.getpgrp(), created
            assert created["inherited_fd"] == 0, created
            retained_fd = created["retained_fd"]
            assert isinstance(retained_fd, int) and retained_fd > 2, created
            descendant_group = created_group

            control.wait_for(operation, "parent_waiting_for_stdin")
            probe_start = len(client.transcript)
            expected_bytes = 0
            chunk_bytes = 64 * 1024
            while True:
                assert expected_bytes + chunk_bytes <= PENDING_TEXT_BUDGET, (
                    "worker stdin remained fully buffered through the adaptive probe; "
                    + control.diagnostics()
                )
                request = client._next_request_id
                client.send(stdin="x" * chunk_bytes, timeout_ms=0)
                probe = client.transcript[-1]
                assert probe["id"] == request, probe
                assert last_tool_text(client) == (
                    "\n[running; poll with an empty send]"
                )
                expected_bytes += chunk_bytes
                control.send_control(
                    operation,
                    "probe_stdin",
                    request=request,
                    expected_bytes=expected_bytes,
                )
                observed = control.wait_for_any(
                    request,
                    {"stdin_write_buffered", "stdin_write_pending"},
                )
                assert observed["target_operation"] == operation, observed
                assert observed["expected_bytes"] == expected_bytes, observed
                consumed_bytes = observed["consumed_bytes"]
                queued_bytes = observed["queued_bytes"]
                assert isinstance(consumed_bytes, int), observed
                assert isinstance(queued_bytes, int) and queued_bytes > 0, observed
                assert consumed_bytes + queued_bytes <= expected_bytes, observed
                if observed["kind"] == "stdin_write_pending":
                    assert consumed_bytes + queued_bytes < expected_bytes, observed
                    break
                assert consumed_bytes + queued_bytes == expected_bytes, observed
                chunk_bytes *= 2

            probes = client.transcript[probe_start:]
            adaptive_probe = probes[0]
            adaptive_probe["send"]["stdin"] = "<adaptive stdin probe>"
            adaptive_probe["result"] = probes[-1]["result"]
            client.transcript[probe_start:] = [adaptive_probe]

            stalled_event = control.wait_for(operation, "parent_operation_stalled")
            stalled_group = stalled_event["process_group"]
            assert isinstance(stalled_group, int) and stalled_group > 0, stalled_event
            assert stalled_group != os.getpgrp(), stalled_event
            assert stalled_group != descendant_group, stalled_event
            worker_group = stalled_group

            poll_stdin = "p" + "x" * (LARGE_OUTPUT_SIZE - 1)
            stalled = client._start_send(
                stdin=poll_stdin,
                timeout_ms=30_000,
            )
            stalled["send"]["stdin"] = "<poll ownership stdin>"
            control.send_control(
                operation,
                "observe_poll_ownership",
                request=stalled["id"],
                prior_bytes=expected_bytes,
                submitted_bytes=len(poll_stdin),
                sentinel=poll_stdin[0],
            )
            ownership = control.wait_for(stalled["id"], "poll_ownership_observed")
            assert ownership["target_operation"] == operation, ownership
            assert ownership["consumed_bytes"] == expected_bytes + 1, ownership
            assert ownership["submitted_bytes"] == len(poll_stdin), ownership
            ownership_queued = ownership["queued_bytes"]
            assert isinstance(ownership_queued, int) and ownership_queued > 0, ownership
            assert ownership_queued < len(poll_stdin) - 1, ownership
            client.send(timeout_ms=0)
            polling = client.transcript[-1]
            assert polling["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": "[worker evaluation is already being polled]",
                    }
                ],
                "isError": True,
            }, polling

            shutdown_started = time.monotonic()
            client.stdin.close()
            try:
                return_code = client.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    "mcp-console did not stop with detached worker stdin; "
                    + control.diagnostics()
                ) from None
            shutdown_elapsed = time.monotonic() - shutdown_started
            server_stopped = True

            client._receive(stalled)
            assert stalled["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": "[worker stopped before operation completed]",
                    }
                ],
                "isError": True,
            }, stalled
            stalled["id"] = "<pending poll request>"
            polling["id"] = "<poll ownership request>"
            standard_error = client.stderr.read()
            assert return_code == 0, standard_error
            assert client.stdout.read() == ""
            assert standard_error == ""
            assert shutdown_elapsed < 2, (
                f"worker shutdown took {shutdown_elapsed:.3f} seconds; "
                + control.diagnostics()
            )
            assert not process_group_exists(worker_group), (
                "worker process group outlived mcp-console shutdown; "
                + control.diagnostics()
            )
            assert not process_group_exists(descendant_group), (
                "detached stdin descendant outlived mcp-console shutdown; "
                + control.diagnostics()
            )

            control.wait_for_eof()
            descendant_retired = True
            return client.transcript
        finally:
            if not server_stopped:
                stop_process(client.process)
            if not descendant_retired:
                stop_process_group(descendant_group)
            stop_process_group(worker_group)


if __name__ == "__main__":
    run_this_suite(__file__)
