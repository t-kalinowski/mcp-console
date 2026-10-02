"""A complete immutable Console installation owned by one case process."""

import shutil
import tempfile
from functools import cache
from pathlib import Path


@cache
def _installed_console(binary: Path) -> tuple[tempfile.TemporaryDirectory, Path]:
    temporary = tempfile.TemporaryDirectory(prefix="mcp-console-test-installation-")
    prefix = Path(temporary.name)
    try:
        (prefix / "bin").mkdir()
        installed = prefix / "bin/mcp-console"
        shutil.copy2(binary, installed)
        for relative in ("libexec", "share/licenses/mcp-console"):
            shutil.copytree(binary.parent.parent / relative, prefix / relative)
    except BaseException:
        temporary.cleanup()
        raise
    return temporary, installed


def installed_console(binary: Path) -> Path:
    # Retain the directory across sequential execution modes, without sharing
    # workspaces, homes, extension caches or activated Python environments.
    return _installed_console(binary)[1]
