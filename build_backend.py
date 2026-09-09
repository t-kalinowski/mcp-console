"""Prepare the private companion before Maturin builds a wheel."""

from __future__ import annotations

import fcntl
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


@contextmanager
def _staged_companion() -> Iterator[None]:
    root = Path(__file__).resolve().parent
    target = root / "target"
    target.mkdir(exist_ok=True)
    # Staging and archiving share one owner. Hold the lock until Maturin has
    # finished consuming wheel-data, including when Cargo reuses its output.
    with (target / "wheel-build.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        subprocess.run(
            [sys.executable, str(root / "scripts/stage-sandbox-runner")], check=True
        )
        yield
