"""Prepare the private companion before Maturin builds a wheel."""

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import maturin

get_requires_for_build_editable = maturin.get_requires_for_build_editable
get_requires_for_build_sdist = maturin.get_requires_for_build_sdist
get_requires_for_build_wheel = maturin.get_requires_for_build_wheel
prepare_metadata_for_build_editable = maturin.prepare_metadata_for_build_editable
prepare_metadata_for_build_wheel = maturin.prepare_metadata_for_build_wheel


def build_sdist(
    sdist_directory: str, config_settings: dict[str, Any] | None = None
) -> str:
    with _checkout_owner(Path(__file__).resolve().parent):
        return maturin.build_sdist(sdist_directory, config_settings)


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
def _checkout_owner(root: Path) -> Iterator[None]:
    if sys.platform == "win32":
        directory = root / ".dev-workflow"
        directory.mkdir(exist_ok=True)
        with (directory / "checkout.lock").open("a") as lock:
            _lock_windows(lock.fileno())
            yield
    else:
        from checkout_workflow import checkout_owner

        with checkout_owner(root):
            yield


@contextmanager
def _staged_companion() -> Iterator[None]:
    root = Path(__file__).resolve().parent
    with _checkout_owner(root):
        if sys.platform == "win32":
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
            subprocess.run(
                [sys.executable, str(root / "scripts/stage-sandbox-runner")], check=True
            )
        yield
