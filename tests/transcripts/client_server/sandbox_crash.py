#!/usr/bin/env -S uv run --script

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import McpClient, Transcript, code, run_this_suite, stop_client


PLATFORMS = {"darwin"}
TIMEOUT = 10


def _last_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    return content[0]["text"]


def _spawn_processx_generation(client: McpClient) -> tuple[int, int, int, Path]:
    # fmt: r
    r = code(r"""
        crash_child <- processx::process$new(
          "/bin/sleep",
          "60",
          stdout = "|",
          stderr = "|",
          cleanup = FALSE
        )
        writeLines(c(
          sprintf("worker=%d", Sys.getpid()),
          sprintf("relay=%d", ps::ps_ppid()),
          sprintf("child=%d", crash_child$get_pid()),
          sprintf("temp=%s", Sys.getenv("TMPDIR"))
        ))
        """)
    client.send(r=r, requirements={"r": ["processx"]})

    result = client.transcript[-1]["result"]
    text = _last_text(client)
    pattern = re.compile(r"(?m)^worker=(\d+)\nrelay=(\d+)\nchild=(\d+)\ntemp=(.+)\n$")
    match = pattern.search(text)
    assert match is not None, text
    worker_pid, relay_pid, child_pid = map(int, match.group(1, 2, 3))
    assert os.getpgid(child_pid) != os.getpgid(worker_pid), (
        "processx child did not leave the worker process group"
    )
    temporary_directory = Path(match.group(4))
    normalized = (
        "worker=<worker pid>\n"
        "relay=<relay pid>\n"
        "child=<processx child pid>\n"
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
    return relay_pid, worker_pid, child_pid, temporary_directory


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_survivors(pids: tuple[int, ...], timeout: float) -> list[int]:
    deadline = time.monotonic() + timeout
    survivors = list(pids)
    while survivors and time.monotonic() < deadline:
        survivors = [pid for pid in survivors if _pid_is_alive(pid)]
        if survivors:
            time.sleep(0.01)
    return [pid for pid in survivors if _pid_is_alive(pid)]


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


def _child_pid_by_name(parent_pid: int, name: str) -> int:
    processes = subprocess.check_output(
        ["ps", "-axo", "pid=,ppid=,comm="],
        text=True,
    )
    matches = []
    for process in processes.splitlines():
        fields = process.strip().split(None, 2)
        if (
            len(fields) == 3
            and int(fields[1]) == parent_pid
            and Path(fields[2]).name == name
        ):
            matches.append(int(fields[0]))
    assert len(matches) == 1, (parent_pid, name, matches)
    return matches[0]


def _runner_lifetime_processes(server_pid: int) -> tuple[int, int]:
    runner_pid = _child_pid_by_name(server_pid, "mcp-console-sandbox")
    manager_pid = _child_pid_by_name(runner_pid, "mcp-console-sandbox")
    return runner_pid, manager_pid


def _kill_if_alive(pid: int | None) -> bool:
    if pid is None or not _pid_is_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


def _close_client_streams(client: McpClient) -> None:
    for stream in (client.stdin, client.stdout, client.stderr):
        try:
            stream.close()
        except BrokenPipeError:
            pass


def test_server_crash_retires_the_worker_generation(binary: Path) -> Transcript:
    # The private sandbox runner must treat loss of the server as retirement
    # of the entire worker generation. A detached processx child must not
    # survive merely because the server received an uncatchable signal before
    # it could run its normal shutdown path.
    client = McpClient(binary, ("serve",))
    generation: tuple[int, int, int, Path] | None = None
    lifetime: tuple[int, int] | None = None
    try:
        client._initialize_and_list_tools()
        generation = _spawn_processx_generation(client)
        lifetime = _runner_lifetime_processes(client.process.pid)

        os.kill(client.process.pid, signal.SIGKILL)
        returncode = client.process.wait(timeout=TIMEOUT)
        survivors = _wait_for_survivors((*generation[:3], *lifetime), timeout=5)

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
            for pid in generation[:3]:
                _kill_if_alive(pid)
            shutil.rmtree(generation[3], ignore_errors=True)
        if lifetime is not None:
            for pid in lifetime:
                _kill_if_alive(pid)
        _close_client_streams(client)


def test_relay_crash_retires_the_worker_generation(binary: Path) -> Transcript:
    # The host-side sandbox lifetime owner must retire the relay and every
    # observed descendant when the relay itself exits. Cleanup cannot depend on
    # code running inside the relay.
    client = McpClient(binary, ("serve",))
    generation: tuple[int, int, int, Path] | None = None
    lifetime: tuple[int, int] | None = None
    try:
        client._initialize_and_list_tools()
        generation = _spawn_processx_generation(client)
        lifetime = _runner_lifetime_processes(client.process.pid)

        os.kill(generation[0], signal.SIGKILL)
        client.transcript.append({"relay_signal": "SIGKILL"})
        _wait_for_generation_failure(client)
        client.send(r=code('writeLines("replacement ready")'))
        replacement = _last_text(client)
        assert replacement == "[starting new worker]\nreplacement ready\n", repr(
            replacement
        )
        survivors = _wait_for_survivors((*generation[:3], *lifetime), timeout=5)
        survivor_names = [
            name
            for name, pid in zip(("relay", "worker", "processx child"), generation[:3])
            if pid in survivors
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
            for pid in generation[:3]:
                _kill_if_alive(pid)
            shutil.rmtree(generation[3], ignore_errors=True)
        if lifetime is not None:
            for pid in lifetime:
                _kill_if_alive(pid)
        _close_client_streams(client)


def test_manager_crash_retires_the_worker_generation(binary: Path) -> Transcript:
    # The outer private runner must retire the live target generation if its
    # internal lifetime process exits unexpectedly.
    client = McpClient(binary, ("serve",))
    generation: tuple[int, int, int, Path] | None = None
    runner_pid: int | None = None
    manager_pid: int | None = None
    try:
        client._initialize_and_list_tools()
        generation = _spawn_processx_generation(client)
        runner_pid, manager_pid = _runner_lifetime_processes(client.process.pid)

        os.kill(manager_pid, signal.SIGKILL)
        client.transcript.append({"manager_signal": "SIGKILL"})
        _wait_for_generation_failure(client)
        client.send(r=code('writeLines("replacement ready")'))
        replacement = _last_text(client)
        assert replacement == "[starting new worker]\nreplacement ready\n", repr(
            replacement
        )
        survivors = _wait_for_survivors((*generation[:3], runner_pid), timeout=5)
        survivor_names = [
            name
            for name, pid in zip(("relay", "worker", "processx child"), generation[:3])
            if pid in survivors
        ]

        assert survivors == [], (
            f"worker-generation processes survived manager crash: {survivor_names}"
        )
        assert not generation[3].exists(), (
            f"worker temporary directory survived manager crash: {generation[3]}"
        )
        return client.transcript
    finally:
        stop_client(client)
        if generation is not None:
            for pid in generation[:3]:
                _kill_if_alive(pid)
            shutil.rmtree(generation[3], ignore_errors=True)
        _kill_if_alive(manager_pid)
        _kill_if_alive(runner_pid)
        _close_client_streams(client)


if __name__ == "__main__":
    run_this_suite(__file__)
