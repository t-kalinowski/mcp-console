#!/usr/bin/env -S uv run --script

import fcntl
import os
import re
import shutil
import signal
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import (
    McpClient,
    Transcript,
    code,
    host_child_pid_by_name,
    linux_host_pid,
    run_this_suite,
    stop_client,
)


PLATFORMS = {"darwin", "linux"}


def _last_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    return content[0]["text"]


def _normalize_generation(client: McpClient) -> tuple[int, int, int, Path]:
    result = client.transcript[-1]["result"]
    text = _last_text(client)
    pattern = re.compile(r"(?m)^worker=(\d+)\nrelay=(\d+)\nchild=(\d+)\ntemp=(.+)\n$")
    match = pattern.search(text)
    assert match is not None, text
    worker_pid, relay_pid, child_pid = map(int, match.group(1, 2, 3))
    if sys.platform == "linux":
        runner_pid = host_child_pid_by_name(client.process.pid, "mcp-console-sandbox")
        worker_pid, relay_pid, child_pid = (
            linux_host_pid(runner_pid, sandbox_pid)
            for sandbox_pid in (worker_pid, relay_pid, child_pid)
        )
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


def _kill_if_alive(pid: int) -> bool:
    if not _pid_is_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


def _kill_generation(generation: tuple[int, int, int, Path]) -> list[str]:
    relay_pid, worker_pid, child_pid, _ = generation
    processes = (
        ("relay", relay_pid),
        ("worker", worker_pid),
        ("processx child", child_pid),
    )
    survivors = []
    for name, pid in processes:
        if _kill_if_alive(pid):
            survivors.append(name)
    return survivors


def _spawn_processx_generation(client: McpClient) -> tuple[int, int, int, Path]:
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
    generation: tuple[int, int, int, Path] | None = None
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
    generation: tuple[int, int, int, Path] | None = None
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


def test_worker_closes_unlisted_server_descriptors(binary: Path) -> Transcript:
    # fmt: python
    python = code(r"""
        import errno
        import os

        descriptor = int(os.environ["MCP_CONSOLE_TEST_INHERITED_FD"])
        try:
            os.write(descriptor, b"escaped")
        except OSError as error:
            assert error.errno == errno.EBADF
        else:
            raise RuntimeError("unlisted server descriptor reached the worker")

        print("closed")
        """)

    with TemporaryDirectory() as directory:
        host_file = Path(directory) / "host.txt"
        host_file.write_bytes(b"")
        with host_file.open("ab", buffering=0) as stream:
            descriptor = fcntl.fcntl(stream.fileno(), fcntl.F_DUPFD, 64)
            os.set_inheritable(descriptor, True)
            environment = os.environ.copy()
            environment["MCP_CONSOLE_TEST_INHERITED_FD"] = str(descriptor)
            client = McpClient(
                binary,
                ("serve",),
                environment=environment,
                pass_fds=(descriptor,),
            )
            try:
                client._initialize_and_list_tools()
                client.send(python=python)
                assert _last_text(client) == "closed\n"
                transcript = client._finish()
            finally:
                stop_client(client)
                os.close(descriptor)
        escaped = host_file.read_bytes()

    assert escaped == b"", escaped
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
