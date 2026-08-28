#!/usr/bin/env -S uv run --script

import os
import re
import shutil
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
    generation: tuple[int, int, int, Path] | None = None
    try:
        client._initialize_and_list_tools()
        generation = _spawn_processx_generation(client)

        os.kill(client.process.pid, signal.SIGKILL)
        returncode = client.process.wait(timeout=TIMEOUT)
        survivors = _wait_for_survivors(generation[:3], timeout=5)

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
        _close_client_streams(client)


if __name__ == "__main__":
    run_this_suite(__file__)
