#!/usr/bin/env -S uv run --script

import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import (
    DarwinProcessIdentity,
    McpClient,
    Transcript,
    capture_darwin_process_identity,
    code,
    kill_darwin_processes,
    run_this_suite,
    stop_client,
)

PLATFORMS = {"darwin"}
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


def _normalize_generation(client: McpClient) -> Generation:
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
    relay_identity = capture_darwin_process_identity(relay_pid)
    worker_identity = capture_darwin_process_identity(worker_pid)
    child_identity = capture_darwin_process_identity(child_pid)
    return relay_identity, worker_identity, child_identity, temporary_directory


def _kill_generation(generation: Generation) -> list[str]:
    survivor_pids = kill_darwin_processes(generation[:3])
    return [
        name
        for name, identity in zip(("relay", "worker", "processx child"), generation[:3])
        if identity[0] in survivor_pids
    ]


def _spawn_processx_generation(client: McpClient) -> Generation:
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
    generation: Generation | None = None
    try:
        client._initialize_and_list_tools()
        generation = _spawn_processx_generation(client)
        client.send(control="restart")
        survivors = _kill_generation(generation)
        temporary_directory = generation[3]
        assert survivors == [], f"old worker generation survived restart: {survivors}"
        assert not temporary_directory.exists(), (
            f"old worker temporary directory survived restart: {temporary_directory}"
        )
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
    generation: Generation | None = None
    try:
        client._initialize_and_list_tools()
        generation = _spawn_processx_generation(client)
        transcript = client._finish()
        survivors = _kill_generation(generation)
        temporary_directory = generation[3]
        assert survivors == [], f"worker generation survived shutdown: {survivors}"
        assert not temporary_directory.exists(), (
            f"worker temporary directory survived shutdown: {temporary_directory}"
        )
        return transcript
    finally:
        stop_client(client)
        if generation is not None:
            _kill_generation(generation)
            shutil.rmtree(generation[3], ignore_errors=True)


if __name__ == "__main__":
    run_this_suite(__file__)
