#!/usr/bin/env -S uv run --script

import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.checkpoints import FifoCheckpoint
from support.client import McpClient, stop_client
from support.macos import (
    DarwinProcessIdentity,
    build_manager_interposer,
    capture_darwin_process_identity,
    darwin_child_process_identities,
    darwin_process_waits_for_control,
    darwin_process_waits_for_startup_release,
    kill_darwin_processes,
    live_darwin_processes,
    signal_darwin_process,
)
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite

PLATFORMS = {"darwin"}
TIMEOUT = 10
Generation = tuple[
    DarwinProcessIdentity,
    DarwinProcessIdentity,
    DarwinProcessIdentity,
    Path,
]


def _last_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    return content[0]["text"]


def _spawn_detached_generation(
    client: McpClient,
    fixture_command: str | None = None,
) -> Generation:
    # Use the bundled Python runtime and standard library so crash supervision
    # does not depend on an externally resolved R package. Starting a new
    # session proves that observation is not limited to the relay's group.
    # fmt: python
    python = code(r"""
        import os
        import subprocess

        crash_child = subprocess.Popen(
            ["/bin/sleep", "60"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(f"worker={os.getpid()}")
        print(f"relay={os.getppid()}")
        print(f"child={crash_child.pid}")
        print(f"temp={os.environ['TMPDIR']}")
        """)
    if fixture_command is None:
        client.send(python=python)
    else:
        client.send(r=fixture_command)

    result = client.transcript[-1]["result"]
    text = _last_text(client)
    pattern = re.compile(r"(?m)^worker=(\d+)\nrelay=(\d+)\nchild=(\d+)\ntemp=(.+)\n$")
    match = pattern.search(text)
    assert match is not None, text
    worker_pid, relay_pid, child_pid = map(int, match.group(1, 2, 3))
    assert os.getsid(child_pid) != os.getsid(worker_pid), (
        "detached child did not leave the worker session"
    )
    temporary_directory = Path(match.group(4))
    normalized = (
        "worker=<worker pid>\n"
        "relay=<relay pid>\n"
        "child=<detached child pid>\n"
        "temp=<sandbox temp>\n"
    )
    result["content"][0]["text"] = (
        text[: match.start()] + normalized + text[match.end() :]
    )
    client.transcript[-1]["transcript_normalization"] = {
        "target": "result.content[0].text",
        "process_ids": "omitted",
        "sandbox_temporary_directory": "omitted",
    }
    relay_identity = capture_darwin_process_identity(relay_pid)
    worker_identity = capture_darwin_process_identity(worker_pid)
    child_identity = capture_darwin_process_identity(child_pid)
    return relay_identity, worker_identity, child_identity, temporary_directory


def _wait_for_generation_cleanup(
    generation: Generation,
    timeout: float,
    additional: tuple[DarwinProcessIdentity, ...] = (),
) -> list[int]:
    identities = (*generation[:3], *additional)
    deadline = time.monotonic() + timeout
    survivors = live_darwin_processes(identities)
    while (survivors or generation[3].exists()) and time.monotonic() < deadline:
        survivors = live_darwin_processes(identities)
        if survivors or generation[3].exists():
            time.sleep(0.01)
    return live_darwin_processes(identities)


def _wait_for_process_cleanup(
    identities: tuple[DarwinProcessIdentity, ...],
    timeout: float,
) -> list[int]:
    deadline = time.monotonic() + timeout
    survivors = live_darwin_processes(identities)
    while survivors and time.monotonic() < deadline:
        time.sleep(0.01)
        survivors = live_darwin_processes(identities)
    return survivors


def _wait_for_private_startup_gate(identity: DarwinProcessIdentity) -> None:
    deadline = time.monotonic() + TIMEOUT
    while not darwin_process_waits_for_startup_release(identity):
        assert live_darwin_processes((identity,)), (
            "custom relay root exited before reaching its private startup gate"
        )
        assert time.monotonic() < deadline, (
            "custom relay root did not block at its private startup gate"
        )
        time.sleep(0.01)


def _wait_for_manager_owner(
    identity: DarwinProcessIdentity,
    temporary_directory: Path,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert temporary_directory.exists(), (
            "manager removed the temporary directory before server retirement"
        )
        if darwin_process_waits_for_control(identity):
            assert temporary_directory.exists()
            return
        time.sleep(0.01)
    raise AssertionError("manager did not wait for server retirement")


def _wait_for_generation_failure(client: McpClient) -> None:
    deadline = time.monotonic() + 5
    poll_start = len(client.transcript)
    while True:
        result = client.send()
        if result.get("isError") is True:
            assert result["content"][0]["text"] == (
                "[worker relay stdout closed before retirement completed]\n"
                "[worker stopped: in-memory state lost]"
            ), result
            final_poll = client.transcript[-1]
            client.transcript[poll_start:] = [final_poll]
            return
        assert time.monotonic() < deadline, (
            "server did not retire the failed generation"
        )
        time.sleep(0.01)


def _manager_pid(server_pid: int) -> int:
    processes = subprocess.check_output(
        ["ps", "-axo", "pid=,ppid=,command="],
        text=True,
    )
    managers = []
    for process in processes.splitlines():
        fields = process.strip().split(None, 2)
        if (
            len(fields) == 3
            and int(fields[1]) == server_pid
            and "sandbox-manager" in fields[2]
        ):
            managers.append(int(fields[0]))
    assert len(managers) == 1, managers
    return managers[0]


def _close_client_streams(client: McpClient) -> None:
    for stream in (client.stdin, client.stdout, client.stderr):
        try:
            stream.close()
        except BrokenPipeError:
            pass


def test_server_crash_retires_the_worker_generation(binary: Path) -> Transcript:
    # The host-side sandbox manager must treat loss of the server as retirement
    # of the entire worker generation. A detached child must not
    # survive merely because the server received an uncatchable signal before
    # it could run its normal shutdown path.
    client = McpClient(binary, ("serve",))
    generation: Generation | None = None
    manager_identity: DarwinProcessIdentity | None = None
    try:
        client._initialize_and_list_tools()
        generation = _spawn_detached_generation(client)
        manager_identity = capture_darwin_process_identity(
            _manager_pid(client.process.pid)
        )

        client.process.kill()
        returncode = client.process.wait(timeout=TIMEOUT)
        survivors = _wait_for_generation_cleanup(
            generation,
            timeout=5,
            additional=(manager_identity,),
        )

        assert returncode == -signal.SIGKILL, returncode
        assert survivors == [], f"worker-generation processes survived: {survivors}"
        assert not generation[3].exists(), (
            f"worker temporary directory survived server crash: {generation[3]}"
        )
        client.transcript.append(
            {
                "server_signal": "SIGKILL",
                "server_returncode": returncode,
            }
        )
        return client.transcript
    finally:
        stop_client(client)
        if generation is not None:
            kill_darwin_processes(generation[:3])
            shutil.rmtree(generation[3], ignore_errors=True)
        if manager_identity is not None:
            kill_darwin_processes((manager_identity,))
        _close_client_streams(client)


def test_server_crash_after_relay_exit_removes_temporary_directory(
    binary: Path,
) -> Transcript:
    # A live but stopped server cannot complete normal relay retirement. The
    # ready manager must retain directory ownership after the relay exits
    # so a later server crash still completes cleanup.
    temporary_owner = tempfile.TemporaryDirectory()
    temporary = Path(temporary_owner.name)
    group_closed = FifoCheckpoint.create(temporary / "manager-group-closed")
    killpg_marker = temporary / "manager-killpg-denied"
    late_member_marker = temporary / "late-process-group-member"
    late_member_reap_marker = temporary / "late-process-group-member-reaped"
    environment = os.environ.copy()
    environment["MCP_CONSOLE_TEST_MANAGER_GROUP_CLOSED"] = str(group_closed.path)
    environment["MCP_CONSOLE_TEST_KILLPG_MARKER"] = str(killpg_marker)
    environment["MCP_CONSOLE_TEST_LATE_MEMBER_MARKER"] = str(late_member_marker)
    environment["MCP_CONSOLE_TEST_LATE_MEMBER_REAP_MARKER"] = str(
        late_member_reap_marker
    )
    environment["DYLD_INSERT_LIBRARIES"] = str(build_manager_interposer(temporary))
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(binary, ("serve", "--worker", str(zod)), environment)
    generation: Generation | None = None
    manager_identity: DarwinProcessIdentity | None = None
    server_identity: DarwinProcessIdentity | None = None
    try:
        client._initialize_and_list_tools()
        generation = _spawn_detached_generation(
            client,
            fixture_command="spawn detached sandbox crash child",
        )
        manager_identity = capture_darwin_process_identity(
            _manager_pid(client.process.pid)
        )
        server_identity = capture_darwin_process_identity(client.process.pid)

        assert signal_darwin_process(server_identity, signal.SIGSTOP), (
            "server exited before stop injection"
        )
        client.transcript.append({"server_signal": "SIGSTOP"})
        assert signal_darwin_process(generation[0], signal.SIGKILL), (
            "relay exited before crash injection"
        )
        client.transcript.append({"relay_signal": "SIGKILL"})
        # The stopped server retains the killed relay as a waitable zombie.
        # Its worker and detached descendant must still be retired before the
        # server can run any cleanup code.
        survivors = _wait_for_process_cleanup(generation[1:3], timeout=5)
        assert survivors == [], f"worker-generation processes survived: {survivors}"
        # Close the root group while the stopped server still pins the waitable
        # relay identity. This backstop covers a same-group child that raced
        # descendant observation.
        group_closed.wait("manager root-group backstop", timeout=5)
        # Once cleanup consumes the manager's kqueue, its sole thread blocks on
        # the server control stream. This positive checkpoint rejects both an
        # active cleanup pass and an exited manager left as a zombie.
        _wait_for_manager_owner(manager_identity, generation[3], timeout=5)
        assert int(killpg_marker.read_text(encoding="utf-8")) == generation[0][0]
        late_member, late_member_group = map(
            int,
            late_member_marker.read_text(encoding="utf-8").split(),
        )
        assert late_member > 0, "invalid late process-group member PID"
        assert late_member_group == generation[0][0], (
            "late member joined a different process group"
        )
        assert int(late_member_reap_marker.read_text(encoding="utf-8")) == (
            late_member
        ), "manager cleanup returned before reaping the late group member"
        assert generation[3].exists()

        client.process.kill()
        returncode = client.process.wait(timeout=TIMEOUT)
        survivors = _wait_for_generation_cleanup(
            generation,
            timeout=5,
            additional=(manager_identity,),
        )

        assert returncode == -signal.SIGKILL, returncode
        assert survivors == [], f"sandbox processes survived: {survivors}"
        assert not generation[3].exists(), (
            f"worker temporary directory survived server crash: {generation[3]}"
        )
        client.transcript.append(
            {
                "server_signal": "SIGKILL",
                "server_returncode": returncode,
            }
        )
        return client.transcript
    finally:
        if generation is not None:
            assert generation[0][0] != os.getpgrp()
            try:
                os.killpg(generation[0][0], signal.SIGKILL)
            except ProcessLookupError:
                pass
        if server_identity is not None:
            signal_darwin_process(server_identity, signal.SIGKILL)
        stop_client(client)
        if generation is not None:
            kill_darwin_processes(generation[:3])
            shutil.rmtree(generation[3], ignore_errors=True)
        if manager_identity is not None:
            kill_darwin_processes((manager_identity,))
        _close_client_streams(client)
        group_closed.close()
        temporary_owner.cleanup()


def test_relay_crash_retires_the_worker_generation(binary: Path) -> Transcript:
    # The host-side sandbox lifetime owner must retire the relay and every
    # observed descendant when the relay itself exits. Cleanup cannot depend on
    # code running inside the relay.
    client = McpClient(binary, ("serve",))
    generation: Generation | None = None
    try:
        client._initialize_and_list_tools()
        generation = _spawn_detached_generation(client)

        assert signal_darwin_process(generation[0], signal.SIGKILL), (
            "relay exited before crash injection"
        )
        client.transcript.append({"relay_signal": "SIGKILL"})
        _wait_for_generation_failure(client)
        client.send(r=code('writeLines("replacement ready")'))
        replacement = _last_text(client)
        assert replacement == "[starting new worker]\nreplacement ready\n", repr(
            replacement
        )
        survivors = _wait_for_generation_cleanup(generation, timeout=5)
        survivor_names = [
            name
            for name, identity in zip(
                ("relay", "worker", "detached child"), generation[:3]
            )
            if identity[0] in survivors
        ]

        assert survivors == [], (
            f"worker-generation processes survived: {survivor_names}"
        )
        assert not generation[3].exists(), (
            f"worker temporary directory survived relay crash: {generation[3]}"
        )
        return client.transcript
    finally:
        stop_client(client)
        if generation is not None:
            kill_darwin_processes(generation[:3])
            shutil.rmtree(generation[3], ignore_errors=True)
        _close_client_streams(client)


def test_manager_crash_retires_the_worker_generation(binary: Path) -> Transcript:
    # While the relay root remains live and pinned, the host owner must take
    # over bounded cleanup if the ready manager exits.
    client = McpClient(binary, ("serve",))
    generation: Generation | None = None
    manager_identity: DarwinProcessIdentity | None = None
    try:
        client._initialize_and_list_tools()
        generation = _spawn_detached_generation(client)
        manager_pid = _manager_pid(client.process.pid)
        manager_identity = capture_darwin_process_identity(manager_pid)

        assert signal_darwin_process(manager_identity, signal.SIGKILL), (
            "manager exited before crash injection"
        )
        client.transcript.append({"manager_signal": "SIGKILL"})
        _wait_for_generation_failure(client)
        client.send(r=code('writeLines("replacement ready")'))
        replacement = _last_text(client)
        assert replacement == "[starting new worker]\nreplacement ready\n", repr(
            replacement
        )
        survivors = _wait_for_process_cleanup(generation[:3], timeout=5)
        survivor_names = [
            name
            for name, identity in zip(
                ("relay", "worker", "detached child"), generation[:3]
            )
            if identity[0] in survivors
        ]

        assert survivors == [], (
            f"worker-generation processes survived manager crash: {survivor_names}"
        )
        assert generation[3].exists(), "manager recovery removed worker temp"
        return client.transcript
    finally:
        stop_client(client)
        if generation is not None:
            kill_darwin_processes(generation[:3])
            shutil.rmtree(generation[3], ignore_errors=True)
        if manager_identity is not None:
            kill_darwin_processes((manager_identity,))
        _close_client_streams(client)


def test_manager_crash_before_gate_release_retires_the_custom_relay_generation(
    binary: Path,
) -> Transcript:
    fixture_root = Path(__file__).resolve().parents[3] / "fixtures"
    relay = fixture_root / "startup_marker_relay"
    worker = fixture_root / "zod"
    marker_name = "mcp-console-startup-marker"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        gate_ready = FifoCheckpoint.create(temporary / "gate-ready")
        gate_release = FifoCheckpoint.create(temporary / "gate-release")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_BINARY"] = str(binary)
        environment["MCP_CONSOLE_TEST_OWNER_GATE_READY"] = str(gate_ready.path)
        environment["MCP_CONSOLE_TEST_OWNER_GATE_RELEASE"] = str(gate_release.path)
        environment["DYLD_INSERT_LIBRARIES"] = str(build_manager_interposer(temporary))
        client = McpClient(
            binary,
            ("serve", "--worker", str(worker), "--relay", str(relay)),
            environment,
        )
        identities: tuple[DarwinProcessIdentity, ...] = ()
        sandbox_temporary_directory: Path | None = None
        gate_released = False
        root_exit = select.kqueue()
        try:
            client._initialize_and_list_tools()
            waiting = client._start_send(r="42")
            gate_ready.wait("owner startup-gate release")

            manager_identity = capture_darwin_process_identity(
                _manager_pid(client.process.pid)
            )
            server_identity = capture_darwin_process_identity(client.process.pid)
            root_candidates = tuple(
                identity
                for identity in darwin_child_process_identities(server_identity)
                if identity != manager_identity
            )
            assert len(root_candidates) == 1, root_candidates
            root_identity = root_candidates[0]
            identities = (root_identity, manager_identity)
            _wait_for_private_startup_gate(root_identity)
            assert darwin_child_process_identities(root_identity) == (), (
                "custom relay root created children before manager readiness"
            )
            sandbox_directories = tuple(
                temporary.glob(f"mcp-console-tmp-{client.process.pid}-*")
            )
            assert len(sandbox_directories) == 1, sandbox_directories
            sandbox_temporary_directory = sandbox_directories[0]
            assert list(temporary.glob(f"**/{marker_name}")) == [], (
                "custom relay executed before startup-gate release"
            )

            exit_watch = select.kevent(
                root_identity[0],
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_EXIT,
            )
            assert root_exit.control([exit_watch], 0, 0) == []
            assert signal_darwin_process(manager_identity, signal.SIGKILL), (
                "manager exited before gate-release crash injection"
            )
            exit_events = root_exit.control(None, 1, TIMEOUT)
            assert len(exit_events) == 1, (
                "manager recovery did not stop the gated custom-relay root"
            )
            assert exit_events[0].ident == root_identity[0], exit_events[0]

            gate_release.release()
            gate_released = True
            client.transcript.append({"manager_signal": "SIGKILL before gate release"})
            client._receive(waiting)
            result = waiting["result"]
            assert result.get("isError") is True, result
            assert (
                "failed to release sandbox target startup gate"
                in (result["content"][0]["text"])
            ), result
            survivors = _wait_for_process_cleanup(identities, timeout=5)

            assert survivors == [], (
                f"pre-release manager crash leaked custom-relay processes: {survivors}"
            )
            assert sandbox_temporary_directory.exists(), (
                "manager recovery removed sandbox temporary directory: "
                f"{sandbox_temporary_directory}"
            )
            assert list(temporary.glob(f"**/{marker_name}")) == [], (
                "custom relay executed after its manager failed before gate release"
            )
            client.transcript.append(
                {
                    "verified_startup_gate": "custom relay did not execute",
                    "verified_cleanup": "gated relay root and manager",
                    "verified_preservation": "sandbox temp",
                }
            )
            return client._finish()
        finally:
            if not gate_released:
                gate_release.release()
            stop_client(client)
            if identities:
                kill_darwin_processes(identities)
            if sandbox_temporary_directory is not None:
                shutil.rmtree(sandbox_temporary_directory, ignore_errors=True)
            root_exit.close()
            gate_ready.close()
            gate_release.close()
            _close_client_streams(client)


if __name__ == "__main__":
    run_this_suite(__file__)
