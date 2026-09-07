import os
import signal
import subprocess


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
