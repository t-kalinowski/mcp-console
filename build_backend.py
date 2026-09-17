"""Prepare the private companion before Maturin builds a wheel."""

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import maturin

build_sdist = maturin.build_sdist
get_requires_for_build_editable = maturin.get_requires_for_build_editable
get_requires_for_build_sdist = maturin.get_requires_for_build_sdist
get_requires_for_build_wheel = maturin.get_requires_for_build_wheel
prepare_metadata_for_build_editable = maturin.prepare_metadata_for_build_editable
prepare_metadata_for_build_wheel = maturin.prepare_metadata_for_build_wheel


def build_wheel(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    with _staged_companion():
        return maturin.build_wheel(wheel_directory, config_settings, metadata_directory)


def build_editable(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    with _staged_companion():
        return maturin.build_editable(
            wheel_directory, config_settings, metadata_directory
        )


def _lock_windows(descriptor: int) -> None:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    lock_file = ctypes.WinDLL("kernel32", use_last_error=True).LockFileEx
    lock_file.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    lock_file.restype = wintypes.BOOL
    overlapped = Overlapped()
    # The file handle is synchronous. An exclusive lock without
    # LOCKFILE_FAIL_IMMEDIATELY blocks until available, unlike LK_LOCK's retries.
    # Closing the file releases the one-byte lock after the complete build.
    if not lock_file(
        msvcrt.get_osfhandle(descriptor), 2, 0, 1, 0, ctypes.byref(overlapped)
    ):
        raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def _staged_companion() -> Iterator[None]:
    root = Path(__file__).resolve().parent
    target = root / "target"
    target.mkdir(exist_ok=True)
    # Staging and archiving share one owner. Hold the lock until Maturin has
    # finished consuming wheel-data, including when Cargo reuses its output.
    with (target / "wheel-build.lock").open("w") as lock:
        if sys.platform == "win32":
            _lock_windows(lock.fileno())
            # Windows currently packages only the unsandboxed executable. Avoid
            # silently shipping a companion staged for another platform.
            if any(
                (root / "wheel-data/data" / name).exists()
                for name in ("libexec", "share")
            ):
                raise RuntimeError(
                    "Windows builds require a checkout without staged Unix companion files"
                )
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
            subprocess.run(
                [sys.executable, str(root / "scripts/stage-sandbox-runner")], check=True
            )
        yield
