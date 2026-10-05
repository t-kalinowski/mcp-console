"""A complete immutable Console installation owned by one case process."""

import shutil
import os
import tempfile
from functools import cache
from pathlib import Path


@cache
def _installed_console(binary: Path) -> tuple[tempfile.TemporaryDirectory, Path]:
    temporary = tempfile.TemporaryDirectory(prefix="mcp-console-test-installation-")
    prefix = Path(temporary.name)
    try:
        (prefix / "bin").mkdir()
        installed = (
            prefix / "bin" / ("mcp-console.exe" if os.name == "nt" else "mcp-console")
        )
        shutil.copy2(binary, installed)
        # Native Windows builds keep licenses inside the immutable libexec bundle.
        directories = (
            ("libexec",)
            if os.name == "nt"
            else ("libexec", "share/licenses/mcp-console")
        )
        for relative in directories:
            shutil.copytree(binary.parent.parent / relative, prefix / relative)
    except BaseException:
        temporary.cleanup()
        raise
    return temporary, installed


def installed_console(binary: Path) -> Path:
    # Retain the directory across sequential execution modes, without sharing
    # workspaces, homes, extension caches or activated Python environments.
    return _installed_console(binary)[1]
