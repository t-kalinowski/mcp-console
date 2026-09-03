#!/usr/bin/env -S uv run --script

import os
import re
import shutil
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text as _last_text
from support.client import McpClient, stop_client
from support.macos import (
    DarwinProcessIdentity as _ProcessIdentity,
    capture_darwin_process_identity as _capture_identity,
    signal_darwin_process,
)
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite


PLATFORMS = {"darwin"}


_Generation = tuple[_ProcessIdentity, _ProcessIdentity, _ProcessIdentity, Path]


def _normalize_generation(client: McpClient) -> _Generation:
    result = client.transcript[-1]["result"]
    text = _last_text(client)
    pattern = re.compile(r"(?m)^worker=(\d+)\nrelay=(\d+)\nchild=(\d+)\ntemp=(.+)\n$")
    match = pattern.search(text)
    assert match is not None, text
    worker_pid, relay_pid, child_pid = map(int, match.group(1, 2, 3))
    assert os.getsid(child_pid) != os.getsid(worker_pid), (
        "processx child did not leave the worker session"
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
    return (
        _capture_identity(relay_pid),
        _capture_identity(worker_pid),
        _capture_identity(child_pid),
        temporary_directory,
    )


def _kill_if_alive(identity: _ProcessIdentity) -> bool:
    return signal_darwin_process(identity, signal.SIGKILL)


def _kill_generation(generation: _Generation) -> list[str]:
    relay, worker, child, _ = generation
    survivors = []
    for name, identity in (
        ("relay", relay),
        ("worker", worker),
        ("processx child", child),
    ):
        if _kill_if_alive(identity):
            survivors.append(name)
    return survivors


def _assert_generation_retired(generation: _Generation, action: str) -> None:
    survivors = _kill_generation(generation)
    assert survivors == [], f"worker generation survived {action}: {survivors}"
    temporary_directory = generation[3]
    assert not temporary_directory.exists(), (
        f"worker temporary directory survived {action}: {temporary_directory}"
    )


def _spawn_processx_generation(client: McpClient) -> _Generation:
    # processx calls setsid() for this child, so it leaves the relay and worker
    # process group while remaining a descendant of the worker generation.
    # fmt: r
    r = code(r"""
        sandbox_child <- processx::process$new(
          "/bin/sleep",
          "60",
          stdout = "|",
          stderr = "|",
          cleanup = FALSE
        )
        writeLines(c(
          sprintf("worker=%d", Sys.getpid()),
          sprintf("relay=%d", ps::ps_ppid()),
          sprintf("child=%d", sandbox_child$get_pid()),
          sprintf("temp=%s", Sys.getenv("TMPDIR"))
        ))
        """)
    client.send(r=r, requirements={"r": ["processx"]})
    return _normalize_generation(client)


def test_restart_retires_descendants_outside_the_worker_group(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    generation: _Generation | None = None
    try:
        client._initialize_and_list_tools()
        generation = _spawn_processx_generation(client)
        client.send(control="restart")
        _assert_generation_retired(generation, "restart")
        return client._finish()
    finally:
        stop_client(client)
        if generation is not None:
            _kill_generation(generation)
            shutil.rmtree(generation[3], ignore_errors=True)


def test_server_shutdown_retires_descendants_outside_the_worker_group(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    generation: _Generation | None = None
    try:
        client._initialize_and_list_tools()
        generation = _spawn_processx_generation(client)
        transcript = client._finish()
        _assert_generation_retired(generation, "shutdown")
        return transcript
    finally:
        stop_client(client)
        if generation is not None:
            _kill_generation(generation)
            shutil.rmtree(generation[3], ignore_errors=True)


if __name__ == "__main__":
    run_this_suite(__file__)
