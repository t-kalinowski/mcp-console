#!/usr/bin/env -S uv run --script

import os
import re
import signal
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import McpClient, Transcript, code, run_this_suite, stop_client


PLATFORMS = {"darwin"}


def _last_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    return content[0]["text"]


def _normalize_processx_pid(client: McpClient) -> int:
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


def _kill_if_alive(pid: int | None) -> bool:
    if pid is None or not _pid_is_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


def _spawn_processx_child(client: McpClient) -> int:
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
        sandbox_child$get_pid()
        """)
    client.send(r=r, requirements={"r": ["processx"]})
    return _normalize_processx_pid(client)


def test_restart_retires_descendants_outside_the_worker_group(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    pid: int | None = None
    try:
        client._initialize_and_list_tools()
        pid = _spawn_processx_child(client)
        client.send(control="restart")
        survived = _kill_if_alive(pid)
        assert not survived, f"processx child {pid} survived worker restart"
        return client._finish()
    finally:
        stop_client(client)
        _kill_if_alive(pid)


def test_server_shutdown_retires_descendants_outside_the_worker_group(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    pid: int | None = None
    try:
        client._initialize_and_list_tools()
        pid = _spawn_processx_child(client)
        transcript = client._finish()
        survived = _kill_if_alive(pid)
        assert not survived, f"processx child {pid} survived server shutdown"
        return transcript
    finally:
        stop_client(client)
        _kill_if_alive(pid)


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
            descriptor = os.dup(stream.fileno())
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
