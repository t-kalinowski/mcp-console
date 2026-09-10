"""Linux-only namespace fixtures; policy assertions stay in the public suites."""

import ctypes
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys


def inherited_procfs_command(command: list[str]) -> list[str]:
    helper = shutil.which("bwrap")
    assert helper is not None, "the outer namespace fixture requires bwrap on PATH"
    return [
        helper,
        "--unshare-user",
        "--unshare-pid",
        "--bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--ro-bind",
        "/proc/sys",
        "/proc/sys",
        "--",
        *command,
    ]


def nested_namespaces_available() -> bool:
    if sys.platform != "linux" or shutil.which("bwrap") is None:
        return False
    # The masked procfs prevents a fresh nested mount. The inner helper must
    # still be able to create the namespaces used by inherited-procfs execution.
    command = inherited_procfs_command(
        [
            shutil.which("bwrap"),
            "--unshare-user",
            "--unshare-pid",
            "--ro-bind",
            "/",
            "/",
            "--",
            "/bin/true",
        ]
    )
    return subprocess.run(command, capture_output=True, timeout=10).returncode == 0


def fresh_procfs_available() -> bool:
    helper = shutil.which("bwrap")
    if sys.platform != "linux" or helper is None:
        return False
    return (
        subprocess.run(
            [
                helper,
                "--unshare-user",
                "--unshare-pid",
                "--ro-bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--",
                "/bin/true",
            ],
            capture_output=True,
            timeout=10,
        ).returncode
        == 0
    )


def process_events_available() -> bool:
    if sys.platform != "linux" or not hasattr(os, "pidfd_open"):
        return False
    try:
        Path("/proc/self/stat").read_text()
        Path(f"/proc/self/task/{os.getpid()}/children").read_text()
        descriptor = os.pidfd_open(os.getpid())
        try:
            signal.pidfd_send_signal(descriptor, 0)
        finally:
            os.close(descriptor)
        library = ctypes.CDLL(None, use_errno=True)
        descriptor = library.inotify_init1(os.O_CLOEXEC)
        if descriptor < 0:
            return False
        os.close(descriptor)
    except OSError:
        return False
    return True


def landlock_available() -> bool:
    if sys.platform != "linux":
        return False
    # landlock_create_ruleset is syscall 444 on both supported Linux architectures.
    # ABI 3 adds truncate enforcement required by the runner's write policy.
    library = ctypes.CDLL(None, use_errno=True)
    return library.syscall(444, 0, 0, 1) >= 3


def without_landlock(directory: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "fixtures/native/without_landlock.c"
    wrapper = directory / "without-landlock"
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-o",
            str(wrapper),
            str(source),
        ],
        check=True,
    )
    return wrapper


def root_metadata_prefix(directory: Path) -> list[str]:
    # Native root-write policies protect these names even when absent. Supply
    # read-only mount targets in an outer namespace, without creating host /.*
    # directories or changing the policy under test.
    metadata = directory / "metadata"
    metadata.mkdir()
    command = inherited_procfs_command([])
    mounts = []
    for name in (".git", ".agents", ".codex"):
        mounts.extend(["--ro-bind", str(metadata), f"/{name}"])
    return [*command[:-1], *mounts, "--"]
