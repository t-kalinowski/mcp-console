import os
import signal
import subprocess
import sys
from pathlib import Path
from collections.abc import Sequence


def process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_process_id(process_id: int | None) -> None:
    if process_id is None:
        return
    try:
        os.kill(process_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_process_group(process_group: int | None) -> None:
    if process_group is None:
        return
    assert process_group > 0, process_group
    assert process_group != os.getpgrp(), process_group
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


ProcessIdentity = tuple[int, int, int]

if sys.platform == "darwin":
    from support.macos import (
        current_darwin_process_identity as current_process_identity,
        capture_darwin_process_identity as capture_process_identity,
        darwin_child_process_identities as child_process_identities,
        live_darwin_processes as live_processes,
        signal_darwin_process as signal_process,
        kill_darwin_processes as kill_processes,
    )
else:

    def current_process_identity(pid: int) -> ProcessIdentity | None:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            return None
        return (pid, int(fields[19]), 0)

    def capture_process_identity(pid: int) -> ProcessIdentity:
        identity = current_process_identity(pid)
        assert identity is not None, f"process {pid} exited before identity capture"
        return identity

    def child_process_identities(
        parent: ProcessIdentity,
    ) -> tuple[ProcessIdentity, ...]:
        assert current_process_identity(parent[0]) == parent
        children = set()
        for task in Path(f"/proc/{parent[0]}/task").iterdir():
            children.update(map(int, (task / "children").read_text().split()))
        assert current_process_identity(parent[0]) == parent
        return tuple(capture_process_identity(pid) for pid in sorted(children))

    def live_processes(identities: Sequence[ProcessIdentity]) -> list[int]:
        return [
            identity[0]
            for identity in identities
            if current_process_identity(identity[0]) == identity
        ]

    def signal_process(identity: ProcessIdentity, number: int) -> bool:
        try:
            descriptor = os.pidfd_open(identity[0])
        except ProcessLookupError:
            return False
        try:
            if current_process_identity(identity[0]) != identity:
                return False
            try:
                signal.pidfd_send_signal(descriptor, number)
            except ProcessLookupError:
                return False
            return True
        finally:
            os.close(descriptor)

    def kill_processes(identities: Sequence[ProcessIdentity]) -> list[int]:
        survivors = live_processes(identities)
        for identity in identities:
            signal_process(identity, signal.SIGKILL)
        return survivors
