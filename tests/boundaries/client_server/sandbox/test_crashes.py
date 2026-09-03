#!/usr/bin/env -S uv run --script

import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient, stop_client
from support.macos import (
    DarwinProcessIdentity,
    capture_darwin_process_identity,
    kill_darwin_processes,
    live_darwin_processes,
    signal_darwin_process,
)
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite

PLATFORMS = {"darwin"}
TIMEOUT = 10
# Python's select module omits Darwin's deprecated process-reaping flag.
_KQ_NOTE_REAP = 0x10000000
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


def _spawn_detached_generation(client: McpClient) -> Generation:
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
    client.send(python=python)

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


def _wait_for_process_reaping(
    process_events: "select.kqueue",
    identities: tuple[DarwinProcessIdentity, ...],
    timeout: float,
) -> None:
    watched = {identity[0] for identity in identities}
    pending = watched.copy()
    deadline = time.monotonic() + timeout
    while pending:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"processes were not reaped: {sorted(pending)}"
        events = process_events.control(None, len(watched), remaining)
        assert events, f"processes were not reaped: {sorted(pending)}"
        for event in events:
            assert event.ident in watched, event
            assert event.filter == select.KQ_FILTER_PROC, event
            if event.fflags & _KQ_NOTE_REAP:
                pending.remove(event.ident)


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
        ["/bin/ps", "-axo", "pid=,ppid=,command="],
        text=True,
    )
    records = []
    for process in processes.splitlines():
        fields = process.strip().split(None, 2)
        if len(fields) == 3:
            records.append((int(fields[0]), int(fields[1]), fields[2]))

    # The sandbox owner may be the server or an intermediate CLI launcher.
    # Locate the manager by ancestry and its internal executable role.
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


def _close_client_streams(client: McpClient) -> None:
    for stream in (client.stdin, client.stdout, client.stderr):
        try:
            stream.close()
        except BrokenPipeError:
            pass


def test_server_crash_retires_the_worker_generation(binary: Path) -> Transcript:
    # The host-side sandbox manager must treat loss of the server as retirement
    # of the entire worker generation. A detached child must not survive merely
    # because the server received an uncatchable signal before it could run its
    # normal shutdown path.
    client = McpClient(binary, ("serve",))
    generation: Generation | None = None
    manager_identity: DarwinProcessIdentity | None = None
    manager_exit = select.kqueue()
    generation_reaping = select.kqueue()
    try:
        client._initialize_and_list_tools()
        generation = _spawn_detached_generation(client)
        manager_identity = capture_darwin_process_identity(
            _manager_pid(client.process.pid)
        )
        exit_watch = select.kevent(
            manager_identity[0],
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_EXIT,
        )
        assert manager_exit.control([exit_watch], 0, 0) == []
        reap_watches = [
            select.kevent(
                identity[0],
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_EXIT | _KQ_NOTE_REAP,
            )
            for identity in generation[:3]
        ]
        assert generation_reaping.control(reap_watches, 0, 0) == []
        assert live_darwin_processes(generation[:3]) == [
            identity[0] for identity in generation[:3]
        ], "worker generation changed while registering reap watches"

        client.process.kill()
        returncode = client.process.wait(timeout=TIMEOUT)
        events = manager_exit.control(None, 1, TIMEOUT)
        assert len(events) == 1, "sandbox manager did not exit after server crash"
        event = events[0]
        assert event.ident == manager_identity[0], event
        assert event.filter == select.KQ_FILTER_PROC, event
        assert event.fflags & select.KQ_NOTE_EXIT, event
        # The manager treats zombies as stopped, but their new parent may reap
        # them just after manager exit. The pre-registered process watches make
        # that final transition observable without racing a libproc sample.
        _wait_for_process_reaping(generation_reaping, generation[:3], TIMEOUT)

        assert returncode == -signal.SIGKILL, returncode
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
        manager_exit.close()
        generation_reaping.close()


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
        client.send(r=code('writeLines("replacement ready")'))
        replacement = _last_text(client)
        assert replacement == "[starting new worker]\nreplacement ready\n", repr(
            replacement
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


if __name__ == "__main__":
    run_this_suite(__file__)
