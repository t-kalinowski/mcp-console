#!/usr/bin/env -S uv run --script

import os
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient, stop_client
from support.execution import SANDBOXED
from support.native import build_interposer
from support.macos import (
    DarwinProcessIdentity,
    capture_darwin_process_identity,
    darwin_child_process_identities,
    live_darwin_processes,
    signal_darwin_process,
)
from support.normalization import code
from support.processes import (
    process_exists,
    process_group_exists,
    stop_process,
    stop_process_group,
    stop_process_id,
)
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, SANDBOX, requires
from support.suites import run_this_suite

LARGE_OUTPUT_SIZE = 2 * 1024 * 1024

PENDING_TEXT_BUDGET = 8 * 1024 * 1024

FIXTURE_CHECKPOINT_TIMEOUT_SECONDS = 15

from boundaries.client_server._harness import (
    ZodFixtureControl,
    read_worker_group,
    wait_for_marker,
    wait_for_stopped_process,
)
from boundaries.client_server.sandbox._fixtures import (
    launcher_retirement,
)


def _manager_pid(server_pid: int) -> int:
    processes = subprocess.check_output(
        ["/bin/ps", "-axo", "pid=,ppid=,command="],
        text=True,
    )
    records = []
    for process in processes.splitlines():
        fields = process.strip().split(None, 2)
        if len(fields) == 3:
            records.append((int(fields[0]), int(fields[1]), fields[2]))

    descendants = {server_pid}
    while True:
        discovered = {pid for pid, parent, _ in records if parent in descendants}
        if discovered.issubset(descendants):
            break
        descendants.update(discovered)
    managers = [
        pid
        for pid, _, command in records
        if pid in descendants and "sandbox-manager" in command.split()
    ]
    assert len(managers) == 1, managers
    return managers[0]


@requires(SANDBOX)
def test_restart_reports_nonzero_sandbox_launcher_exit(binary: Path) -> Transcript:
    relay = (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "server_relay"
        / "scripted_relay.py"
    )
    environment = os.environ.copy()
    environment["MCP_CONSOLE_TEST_RELAY_SCENARIO"] = "shutdown_nonzero"
    client = McpClient(
        binary,
        SANDBOXED.serve("--worker", str(binary), "--relay", str(relay)),
        environment,
    )
    try:
        client.initialize_and_list_tools()
        client.send(control="restart")
        assert last_tool_text(client) == "[starting new worker]\n[idle]"

        client.send(control="restart")
        return client.transcript
    finally:
        stop_client(client)


@requires(SANDBOX)
def test_restart_rejects_unsolicited_status_137(binary: Path) -> Transcript:
    relay = (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "server_relay"
        / "scripted_relay.py"
    )
    environment = os.environ.copy()
    environment["MCP_CONSOLE_TEST_RELAY_SCENARIO"] = "shutdown_status_137"
    client = McpClient(
        binary,
        SANDBOXED.serve("--worker", str(binary), "--relay", str(relay)),
        environment,
    )
    try:
        client.initialize_and_list_tools()
        client.send(control="restart")
        assert last_tool_text(client) == "[starting new worker]\n[idle]"

        client.send(control="restart")
        return client.transcript
    finally:
        stop_client(client)


@requires(SANDBOX, NATIVE_FIXTURES)
def test_restart_rejects_status_137_when_launcher_exits_before_sigterm(
    binary: Path,
) -> Transcript:
    with launcher_retirement(binary) as retirement:
        client = retirement.client
        retirement.signal_return_release.release()
        restart = client.start_send(control="restart")
        retirement.signal_blocked.wait(
            "server retirement signal", FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
        )
        retirement.relay_exit.release()
        retirement.wait_for_launcher_exit()
        retirement.signal_release.release()
        retirement.signal_returned.wait(
            "successful signal to exited launcher", FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
        )
        client.receive(restart)
        return client.transcript


@requires(SANDBOX, NATIVE_FIXTURES)
def test_restart_accepts_owned_retirement_when_launcher_exits_before_signal_returns(
    binary: Path,
) -> Transcript:
    with launcher_retirement(binary) as retirement:
        client = retirement.client
        restart = client.start_send(control="restart")
        retirement.signal_blocked.wait(
            "server retirement signal", FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
        )
        retirement.assert_launcher_running()
        retirement.signal_release.release()
        retirement.signal_returned.wait(
            "successful retirement signal", FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
        )
        retirement.wait_for_launcher_exit()
        retirement.signal_return_release.release()
        client.receive(restart)
        return client.transcript


@requires(SANDBOX, NATIVE_FIXTURES)
def test_restart_drains_relay_output_before_nonzero_launcher_error(
    binary: Path,
) -> Transcript:
    return _restart_drains_relay_output_before_nonzero_launcher_error(binary, 73)


@requires(SANDBOX, NATIVE_FIXTURES)
def test_restart_drains_relay_output_before_status_one_launcher_error(
    binary: Path,
) -> Transcript:
    return _restart_drains_relay_output_before_nonzero_launcher_error(binary, 1)


def _restart_drains_relay_output_before_nonzero_launcher_error(
    binary: Path,
    status: int,
) -> Transcript:
    relay = (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "server_relay"
        / "scripted_relay.py"
    )
    # fmt: python
    launcher = code(r"""
        import os
        import sys

        os.environ["MCP_CONSOLE_TEST_RELAY_READ_PID"] = str(os.getpid())
        os.environ["DYLD_INSERT_LIBRARIES"] = os.environ.pop(
            "MCP_CONSOLE_TEST_RELAY_READ_DYLIB"
        )
        os.execv(sys.argv[1], sys.argv[1:])
        """)
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        blocked = FifoCheckpoint.create(temporary / "relay-read-blocked")
        release = FifoCheckpoint.create(temporary / "relay-read-release")
        relay_exit = FifoCheckpoint.create(temporary / "relay-exit-release")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_RELAY_SCENARIO"] = "shutdown_nonzero_after_output"
        environment["MCP_CONSOLE_TEST_RELAY_EXIT_STATUS"] = str(status)
        environment["MCP_CONSOLE_TEST_RELAY_READ_DYLIB"] = str(
            build_interposer(temporary, "relay_stdout_read_interposer")
        )
        environment["MCP_CONSOLE_TEST_RELAY_READ_MATCH"] = (
            "old generation retirement output"
        )
        environment["MCP_CONSOLE_TEST_RELAY_READ_BLOCKED"] = str(blocked.path)
        environment["MCP_CONSOLE_TEST_RELAY_READ_RELEASE"] = str(release.path)
        environment["MCP_CONSOLE_TEST_RELAY_EXIT_RELEASE"] = str(relay_exit.path)
        client = McpClient(
            Path(sys.executable),
            (
                "-c",
                launcher,
                str(binary),
                *SANDBOXED.serve("--worker", str(binary), "--relay", str(relay)),
            ),
            environment,
            current_directory=temporary,
        )
        try:
            client.initialize_and_list_tools()
            client.send(control="restart")
            assert last_tool_text(client) == "[starting new worker]\n[idle]"

            restart = client.start_send(control="restart")
            blocked.wait("relay stdout reader", FIXTURE_CHECKPOINT_TIMEOUT_SECONDS)
            relay_exit.release()
            client.receive(restart)
            release.release()
            return client.transcript
        finally:
            relay_exit.release()
            release.release()
            blocked.close()
            release.close()
            relay_exit.close()
            stop_client(client)


@requires(SANDBOX)
def test_restart_preserves_relay_retirement_failure(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(binary, SANDBOXED.serve("--worker", str(zod)))
    try:
        client.initialize_and_list_tools()
        client.send(r="fail sideband during shutdown")
        assert last_tool_text(client) == "[done]"

        client.send(control="restart")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        output = result["content"][0]["text"]
        prefix = "[worker sideband read failed: "
        assert output.startswith(prefix), output
        assert output.endswith("]"), output
        assert "\n" not in output, output
        assert "; additionally" not in output, output
        assert "worker launcher" not in output, output
        assert "[starting new worker]" not in output, output
        result["content"][0]["text"] = prefix + "<invalid frame>]"
        client.transcript[-1]["transcript_normalization"] = {
            "target": "result.content[0].text",
            "replacements": {"sideband_failure_detail": "<invalid frame>"},
        }
        return client.transcript
    finally:
        stop_client(client)


@requires(SANDBOX, PROCESS_EVENTS)
def test_restart_allows_accepted_relay_shutdown_to_finish(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_BINARY"] = str(binary)
        client = McpClient(
            binary,
            SANDBOXED.serve(
                "--worker",
                str(zod),
                "--relay",
                str(zod.with_name("identified_relay")),
            ),
            environment,
        )
        helper_pid = None
        passed = False
        try:
            client.initialize_and_list_tools()
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
            relay_group = os.getpgid(relay_target)
            assert relay_group != relay_target

            restarted = client.start_send(control="restart")
            stopped_marker = wait_for_marker(
                temporary_path,
                "zod-relay-stopped-after-shutdown",
                client,
            )
            wait_for_stopped_process(
                relay_target,
                relay_group,
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
            client.receive(restarted)
            assert not process_exists(helper_pid), (
                "detached relay-resume helper outlived sandbox retirement"
            )
            assert not process_exists(relay_target), "retired relay survived restart"
            assert not process_exists(relay_group), "sandbox runner survived restart"
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
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_id(helper_pid)
                stop_process(client.process)


def _restart_outer_force_stops_unresponsive_relay(
    binary: Path,
    *,
    stop_manager: bool,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        environment["MCP_CONSOLE_TEST_BINARY"] = str(binary)
        client = McpClient(
            binary,
            SANDBOXED.serve(
                "--worker",
                str(zod),
                "--relay",
                str(zod.with_name("identified_relay")),
            ),
            environment,
        )
        helper_pid = None
        worker_group = None
        manager: DarwinProcessIdentity | None = None
        passed = False
        try:
            client.initialize_and_list_tools()
            client.send(r="stall with stopped relay", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            helper_marker = wait_for_marker(
                temporary_path,
                "zod-relay-stop-helper",
                client,
            )
            helper_pid = int(helper_marker.read_text(encoding="utf-8"))
            relay_target, worker_pid = map(
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
            assert os.getpgid(relay_target) == worker_group
            assert relay_target != worker_group, "helper targeted the sandbox runner"
            assert worker_pid != relay_target, (
                "Zod worker unexpectedly identified the relay"
            )
            assert os.getpgid(worker_pid) == worker_group, (
                "Zod worker did not inherit the relay process group"
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
            if stop_manager:
                manager = capture_darwin_process_identity(
                    _manager_pid(client.process.pid)
                )
                assert signal_darwin_process(manager, signal.SIGSTOP), (
                    "sandbox manager exited before the stall injection"
                )

            relay = capture_darwin_process_identity(relay_target)
            root = capture_darwin_process_identity(worker_group)
            assert darwin_child_process_identities(root) == (relay,)
            descendants = [root]
            for process in descendants:
                descendants.extend(darwin_child_process_identities(process))
            retiring = {identity[0] for identity in descendants}
            assert {worker_pid, helper_pid}.issubset(retiring), retiring
            if manager is not None:
                retiring.add(manager[0])
            with closing(select.kqueue()) as exits:
                watches = [
                    select.kevent(
                        pid,
                        filter=select.KQ_FILTER_PROC,
                        flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                        fflags=select.KQ_NOTE_EXIT,
                    )
                    for pid in retiring
                ]
                assert exits.control(watches, 0, 0) == []
                restarted = client.start_send(control="restart")
                retirement_deadline = time.monotonic() + (10 if stop_manager else 5)
                # Descendants must exit; their external parents may retain zombies.
                # The launcher must also reap its own relay and manager below.
                while retiring:
                    events = exits.control(
                        None,
                        len(retiring),
                        max(0, retirement_deadline - time.monotonic()),
                    )
                    assert client.process.poll() is None, (
                        "mcp-console stopped while retiring the sandbox lifetime"
                    )
                    assert events, (
                        f"sandbox processes did not exit within the deadline: {retiring}"
                    )
                    for event in events:
                        assert event.filter == select.KQ_FILTER_PROC, event
                        assert event.fflags & select.KQ_NOTE_EXIT, event
                        retiring.remove(event.ident)
            client.receive(restarted)
            assert live_darwin_processes((root, relay)) == [], (
                "sandbox launcher did not retire its runner and relay"
            )
            if manager is not None:
                assert live_darwin_processes((manager,)) == [], (
                    "stopped sandbox manager outlived launcher recovery"
                )
                restarted["launcher_recovery"] = {
                    "manager": "stopped before owned retirement",
                    "verified_barrier": "manager, relay, worker, and detached descendant",
                }
                passed = True
                return client.transcript
            assert last_tool_text(client) == (
                "[active evaluation stopped by session restart request]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            )

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if manager is not None:
                signal_darwin_process(manager, signal.SIGCONT)
            if stop_manager:
                stop_client(client)
            if not passed:
                stop_process_id(helper_pid)
                stop_process_group(worker_group)
                stop_process(client.process)


@requires(SANDBOX, PROCESS_EVENTS)
def test_restart_outer_force_stops_unresponsive_relay(binary: Path) -> Transcript:
    return _restart_outer_force_stops_unresponsive_relay(
        binary,
        stop_manager=False,
    )


@requires(SANDBOX, PROCESS_EVENTS)
def test_restart_waits_for_owned_launcher_manager_recovery(
    binary: Path,
) -> Transcript:
    return _restart_outer_force_stops_unresponsive_relay(
        binary,
        stop_manager=True,
    )


@requires(SANDBOX, PROCESS_EVENTS)
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
            SANDBOXED.serve("--worker", str(zod)),
            environment,
        )
        descendant_group = None
        passed = False
        try:
            client.initialize_and_list_tools()
            waiting = client.start_send(r="echo echo", timeout_ms=30_000)
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
            restarted = client.start_send(control="restart")
            responses_returned = threading.Event()
            forced_stop = threading.Event()

            def stop_if_calls_block() -> None:
                if not responses_returned.wait(FIXTURE_CHECKPOINT_TIMEOUT_SECONDS):
                    forced_stop.set()
                    stop_process(client.process)

            watchdog = threading.Thread(target=stop_if_calls_block, daemon=True)
            watchdog.start()
            try:
                client.receive(waiting)
                client.receive(restarted)
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
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            startup_release.touch()
            if not passed:
                stop_process_group(descendant_group)
                stop_process(client.process)


@requires(SANDBOX)
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
            SANDBOXED.serve("--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        client.send(r="probe sandbox")
        transcript = client.finish()

        assert host_file.read_text(encoding="utf-8") == "host data"
        return transcript


@requires(SANDBOX)
def test_shutdown_is_bounded_with_detached_stdin_descendant(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment = os.environ.copy()
    with ZodFixtureControl() as control:
        control.configure(environment)
        client = McpClient(
            binary,
            SANDBOXED.serve("--worker", str(zod)),
            environment,
        )
        descendant_group = None
        descendant_retired = False
        worker_group = None
        server_stopped = False
        try:
            client.initialize_and_list_tools()
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
            stalled = client.start_send(
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

            client.receive(stalled)
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
