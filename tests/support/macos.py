import ctypes
import errno
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence


DarwinProcessIdentity = tuple[int, int, int]


class _DarwinProcessInfo(ctypes.Structure):
    # In Darwin's stable proc_bsdinfo ABI, the two start-time fields follow a
    # 120-byte prefix and complete the 136-byte structure.
    _fields_ = [
        ("prefix", ctypes.c_byte * 120),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class _DarwinProcessFdInfo(ctypes.Structure):
    _fields_ = [
        ("fd", ctypes.c_int32),
        ("fdtype", ctypes.c_uint32),
    ]


class _DarwinThreadInfo(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_uint64),
        ("system_time", ctypes.c_uint64),
        ("cpu_usage", ctypes.c_int32),
        ("policy", ctypes.c_int32),
        ("run_state", ctypes.c_int32),
        ("flags", ctypes.c_int32),
        ("sleep_time", ctypes.c_int32),
        ("current_priority", ctypes.c_int32),
        ("priority", ctypes.c_int32),
        ("max_priority", ctypes.c_int32),
        ("name", ctypes.c_char * 64),
    ]


_LIBPROC = None
if sys.platform == "darwin":
    _LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    _LIBPROC.proc_listchildpids.argtypes = [
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    _LIBPROC.proc_listchildpids.restype = ctypes.c_int
    _LIBPROC.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    _LIBPROC.proc_pidinfo.restype = ctypes.c_int


def current_darwin_process_identity(pid: int) -> DarwinProcessIdentity | None:
    assert _LIBPROC is not None
    proc_pidtbsdinfo = 3
    include_zombies = 1
    info = _DarwinProcessInfo()
    ctypes.set_errno(0)
    size = _LIBPROC.proc_pidinfo(
        pid,
        proc_pidtbsdinfo,
        include_zombies,
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


def capture_darwin_process_identity(pid: int) -> DarwinProcessIdentity:
    identity = current_darwin_process_identity(pid)
    assert identity is not None, f"process {pid} exited before identity capture"
    return identity


def live_darwin_processes(
    identities: Sequence[DarwinProcessIdentity],
) -> list[int]:
    return [
        identity[0]
        for identity in identities
        if current_darwin_process_identity(identity[0]) == identity
    ]


def darwin_child_process_identities(
    parent: DarwinProcessIdentity,
) -> tuple[DarwinProcessIdentity, ...]:
    assert _LIBPROC is not None
    assert current_darwin_process_identity(parent[0]) == parent, (
        "parent process exited before child inspection"
    )
    capacity = 16
    while True:
        child_pids = (ctypes.c_int * capacity)()
        ctypes.set_errno(0)
        count = _LIBPROC.proc_listchildpids(
            parent[0],
            child_pids,
            ctypes.sizeof(child_pids),
        )
        error = ctypes.get_errno()
        if count < 0 or (count == 0 and error != 0):
            raise OSError(error, f"failed to list children of process {parent[0]}")
        if count < capacity:
            break
        capacity *= 2

    assert current_darwin_process_identity(parent[0]) == parent, (
        "parent process changed during child inspection"
    )
    return tuple(capture_darwin_process_identity(pid) for pid in child_pids[:count])


def _darwin_process_resources(
    identity: DarwinProcessIdentity,
) -> tuple[set[tuple[int, int]], _DarwinThreadInfo] | None:
    assert _LIBPROC is not None
    if current_darwin_process_identity(identity[0]) != identity:
        return None

    proc_pidlistfds = 1
    proc_pidlistthreads = 6
    proc_pidthreadinfo = 5

    fd_infos = (_DarwinProcessFdInfo * 16)()
    fd_size = _LIBPROC.proc_pidinfo(
        identity[0],
        proc_pidlistfds,
        0,
        fd_infos,
        ctypes.sizeof(fd_infos),
    )
    if fd_size <= 0:
        return None
    assert fd_size % ctypes.sizeof(_DarwinProcessFdInfo) == 0, fd_size
    file_descriptors = {
        (info.fd, info.fdtype)
        for info in fd_infos[: fd_size // ctypes.sizeof(_DarwinProcessFdInfo)]
    }
    thread_ids = (ctypes.c_uint64 * 16)()
    thread_size = _LIBPROC.proc_pidinfo(
        identity[0],
        proc_pidlistthreads,
        0,
        thread_ids,
        ctypes.sizeof(thread_ids),
    )
    if thread_size != ctypes.sizeof(ctypes.c_uint64):
        return None

    thread_info = _DarwinThreadInfo()
    info_size = _LIBPROC.proc_pidinfo(
        identity[0],
        proc_pidthreadinfo,
        thread_ids[0],
        ctypes.byref(thread_info),
        ctypes.sizeof(thread_info),
    )
    if (
        info_size != ctypes.sizeof(thread_info)
        or current_darwin_process_identity(identity[0]) != identity
    ):
        return None
    return file_descriptors, thread_info


def _darwin_main_thread_waits(thread_info: _DarwinThreadInfo) -> bool:
    th_state_waiting = 3
    return (
        thread_info.run_state == th_state_waiting
        and thread_info.name.rstrip(b"\0") == b"main"
    )


def darwin_process_waits_for_startup_release(
    identity: DarwinProcessIdentity,
) -> bool:
    """Return whether the exact target wrapper is waiting on its private gate."""
    prox_fdtype_socket = 2
    resources = _darwin_process_resources(identity)
    if resources is None:
        return False
    file_descriptors, thread_info = resources
    standard_descriptors = {
        descriptor for descriptor, _ in file_descriptors if descriptor <= 2
    }
    extra_descriptors = [
        descriptor_type
        for descriptor, descriptor_type in file_descriptors
        if descriptor > 2
    ]
    return (
        standard_descriptors == {0, 1, 2}
        and extra_descriptors == [prox_fdtype_socket]
        and _darwin_main_thread_waits(thread_info)
    )


def signal_darwin_process(identity: DarwinProcessIdentity, number: int) -> bool:
    # macOS has no pidfd-like signal API. Recheck the start time immediately
    # before signaling so a reused PID is not treated as the test process.
    if current_darwin_process_identity(identity[0]) != identity:
        return False
    try:
        os.kill(identity[0], number)
    except ProcessLookupError:
        return False
    return True


def kill_darwin_processes(
    identities: Sequence[DarwinProcessIdentity],
) -> list[int]:
    survivors = live_darwin_processes(identities)
    for identity in identities:
        signal_darwin_process(identity, signal.SIGKILL)
    return survivors


def wait_for_darwin_process_state(
    identity: DarwinProcessIdentity,
    prefix: str,
    description: str,
    *,
    timeout: float = 10,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        assert live_darwin_processes((identity,)) == [identity[0]], (
            f"{description} exited before reaching state {prefix!r}"
        )
        result = subprocess.run(
            ["/bin/ps", "-o", "state=", "-p", str(identity[0])],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
        if result.stdout.strip().startswith(prefix):
            return
        assert time.monotonic() < deadline, (
            f"timed out waiting for {description} state {prefix!r}"
        )
        time.sleep(0.01)


def wait_for_darwin_startup_release(
    identity: DarwinProcessIdentity,
    description: str,
    *,
    timeout: float = 10,
) -> None:
    deadline = time.monotonic() + timeout
    while not darwin_process_waits_for_startup_release(identity):
        assert live_darwin_processes((identity,)), (
            f"{description} exited before reaching its private startup gate"
        )
        assert time.monotonic() < deadline, (
            f"{description} did not block at its private startup gate"
        )
        time.sleep(0.01)
