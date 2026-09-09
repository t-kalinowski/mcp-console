"""Prepare the private companion before Maturin builds a wheel."""

from __future__ import annotations

import fcntl
import subprocess
import sys
from pathlib import Path
from typing import Any

import maturin
from maturin import build_sdist as build_sdist
from maturin import get_requires_for_build_sdist as get_requires_for_build_sdist
from maturin import get_requires_for_build_wheel as get_requires_for_build_wheel
from maturin import prepare_metadata_for_build_wheel as prepare_metadata_for_build_wheel


def build_wheel(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
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
        return maturin.build_wheel(wheel_directory, config_settings, metadata_directory)
