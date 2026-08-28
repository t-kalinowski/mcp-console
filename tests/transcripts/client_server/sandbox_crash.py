#!/usr/bin/env -S uv run --script

import os
import re
import signal
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


def _spawn_processx_child(client: McpClient) -> int:
    # fmt: r
    r = code(r"""
        crash_child <- processx::process$new(
          "/bin/sleep",
          "60",
          stdout = "|",
          stderr = "|",
          cleanup = FALSE
        )
        crash_child$get_pid()
        """)
    client.send(r=r, requirements={"r": ["processx"]})

    result = client.transcript[-1]["result"]
    text = _last_text(client)
    matches = list(re.finditer(r"(?m)^\[1\] (\d+)\n$", text))
    assert len(matches) == 1, text
    match = matches[0]
    pid = int(match.group(1))
    result["content"][0]["text"] = (
        text[: match.start()] + "[1] <processx child pid>\n" + text[match.end() :]
    )
    return pid


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_survivor(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _pid_is_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    return _pid_is_alive(pid)


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
    # The sandbox relay must treat loss of its host-side owner as retirement of
    # the entire worker generation. A detached processx child must not survive
    # merely because the server received an uncatchable signal before it could
    # run its normal shutdown path.
    client = McpClient(binary, ("serve",))
    child_pid: int | None = None
    try:
        client._initialize_and_list_tools()
        child_pid = _spawn_processx_child(client)

        os.kill(client.process.pid, signal.SIGKILL)
        returncode = client.process.wait(timeout=TIMEOUT)
        survived = _wait_for_survivor(child_pid, timeout=5)

        assert returncode == -signal.SIGKILL, returncode
        assert not survived, f"processx child {child_pid} survived server crash"
        client.transcript.append(
            {
                "server_signal": "SIGKILL",
                "server_returncode": returncode,
            }
        )
        return client.transcript
    finally:
        stop_client(client)
        _kill_if_alive(child_pid)
        _close_client_streams(client)


if __name__ == "__main__":
    run_this_suite(__file__)
