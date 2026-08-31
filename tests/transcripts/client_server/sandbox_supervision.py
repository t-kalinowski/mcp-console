#!/usr/bin/env -S uv run --script

import ctypes
import errno
import os
import re
import shutil
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import McpClient, Transcript, code, run_this_suite, stop_client


PLATFORMS = {"darwin"}
PROC_PIDTBSDINFO = 3
INCLUDE_ZOMBIES = 1


_ProcessIdentity = tuple[int, int, int]
_Generation = tuple[_ProcessIdentity, _ProcessIdentity, _ProcessIdentity, Path]


class _ProcessInfo(ctypes.Structure):
    # In Darwin's stable proc_bsdinfo ABI, the two start-time fields follow a
    # 120-byte prefix and complete the 136-byte structure.
    _fields_ = [
        ("prefix", ctypes.c_byte * 120),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


_LIBPROC = None
if sys.platform == "darwin":
    _LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    _LIBPROC.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    _LIBPROC.proc_pidinfo.restype = ctypes.c_int


def _last_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    return content[0]["text"]


def _process_identity(pid: int) -> _ProcessIdentity | None:
    assert _LIBPROC is not None
    info = _ProcessInfo()
    ctypes.set_errno(0)
    size = _LIBPROC.proc_pidinfo(
        pid,
        PROC_PIDTBSDINFO,
        INCLUDE_ZOMBIES,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if size == ctypes.sizeof(info):
        return (pid, info.pbi_start_tvsec, info.pbi_start_tvusec)
    error = ctypes.get_errno()
    if size == 0 and error == errno.ESRCH:
        return None
    if size == 0 and error != 0:
        raise OSError(error, f"failed to inspect process {pid}")
    raise RuntimeError(
        f"proc_pidinfo returned {size} bytes for process {pid}, "
        f"expected {ctypes.sizeof(info)}"
    )


def _capture_identity(pid: int) -> _ProcessIdentity:
    identity = _process_identity(pid)
    assert identity is not None, f"process {pid} exited before identity capture"
    return identity


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
    pid = identity[0]
    if _process_identity(pid) != identity:
        return False
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


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


def test_failure_replacement_retires_descendants_outside_the_worker_group(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    generation: _Generation | None = None
    try:
        client._initialize_and_list_tools()
        generation = _spawn_processx_generation(client)
        client.send(r="tools::pskill(Sys.getpid(), signal = 9L)")
        result = client.transcript[-1]["result"]
        assert result == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "[worker sideband read failed: worker sideband closed]\n"
                        "[worker terminated by signal 9]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                }
            ],
            "isError": True,
        }, result
        _assert_generation_retired(generation, "failure replacement")
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
