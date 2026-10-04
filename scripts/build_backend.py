"""Prepare the private companion before Maturin builds a wheel."""

import argparse
import json
import os
import runpy
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
    with _checkout_owner(Path(__file__).resolve().parent.parent):
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


@contextmanager
def _checkout_owner(root: Path) -> Iterator[None]:
    from checkout_workflow import checkout_owner

    with checkout_owner(root, wait=sys.platform == "win32"):
        yield


@contextmanager
def _staged_companion() -> Iterator[None]:
    root = Path(__file__).resolve().parent.parent
    with _checkout_owner(root):
        if sys.platform == "win32":
            # Stage in-process under the shared checkout owner; the pinned
            # companion source has its own ownership lock.
            staging = runpy.run_path(str(root / "scripts/stage-sandbox-runner"))
            args = argparse.Namespace(
                checkout=Path(source)
                if (source := os.environ.get("MCP_CONSOLE_SANDBOX_SOURCE"))
                else None,
                target=None,
            )
            staging["stage_locked"](
                args, json.loads((root / "sandbox-runner.json").read_text())
            )
        else:
            subprocess.run(
                [sys.executable, str(root / "scripts/stage-sandbox-runner")], check=True
            )
        yield
