import fcntl
import os
import pty
import selectors
import signal
import termios
import time

TIMEOUT = 10


def _command(*arguments: str) -> list[str]:
    return ["mcp-console", *arguments]


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_survivors(pids: list[int]) -> list[int]:
    survivors = [pid for pid in pids if _pid_is_alive(pid)]
    for pid in survivors:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return survivors


def _kill_process_groups(process_groups: list[int | None]) -> None:
    for process_group in {group for group in process_groups if group is not None}:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _open_controlling_terminal() -> tuple[int, int, object]:
    master, slave = pty.openpty()

    def attach_controlling_terminal() -> None:
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        os.tcsetpgrp(slave, os.getpid())

    return master, slave, attach_controlling_terminal


def _wait_for_stop(process_id: int) -> int:
    deadline = time.monotonic() + TIMEOUT
    while True:
        waited, status = os.waitpid(process_id, os.WUNTRACED | os.WNOHANG)
        if waited == process_id:
            assert os.WIFSTOPPED(status), status
            return status
        assert time.monotonic() < deadline, "timed out waiting for launcher stop"
        time.sleep(0.01)


def _read_lines(stream: object, count: int, description: str) -> list[str]:
    descriptor = stream.fileno()  # type: ignore[attr-defined]
    output = bytearray()
    deadline = time.monotonic() + TIMEOUT
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        while output.count(b"\n") < count:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"timed out waiting for {description}"
            ready = selector.select(remaining)
            assert ready, f"timed out waiting for {description}"
            chunk = os.read(descriptor, 4096)
            assert chunk, f"sandbox closed before reporting {description}"
            output.extend(chunk)
    lines = output.decode("utf-8").splitlines()
    assert len(lines) == count, (description, lines)
    return lines


def _read_until(
    descriptor: int,
    markers: bytes | tuple[bytes, ...],
    description: str,
) -> bytes:
    if isinstance(markers, bytes):
        markers = (markers,)
    output = bytearray()
    deadline = time.monotonic() + TIMEOUT
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        while not all(marker in output for marker in markers):
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"timed out waiting for {description}"
            ready = selector.select(remaining)
            assert ready, f"timed out waiting for {description}"
            chunk = os.read(descriptor, 4096)
            assert chunk, f"terminal closed before reporting {description}"
            output.extend(chunk)
    return bytes(output)
