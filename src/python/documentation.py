"""Worker-local lookup; shared with the explicit host preparation command."""

import os
import sys
from pathlib import Path

MANUAL_INDEXES = (
    "contents.txt",
    "library/index.txt",
    "tutorial/index.txt",
    "reference/index.txt",
)


def documentation_root() -> Path:
    selected = os.environ.get("MCP_CONSOLE_PYTHON_DOCS")
    if selected is None:
        home = os.environ.get("MCP_CONSOLE_HOME")
        selected = str(
            (Path(home) if home is not None else Path.home() / ".agents/console")
            / "python-docs"
        )
    root = Path(selected)
    if not root.is_absolute():
        raise ValueError("Python documentation cache must be an absolute path")
    return root


def python_docs() -> dict[str, object] | None:
    """Return this worker's prepared manual and provenance, or None if absent.

    Host administration: mcp-console prepare-python-docs --python <sys.executable>.
    Set MCP_CONSOLE_PYTHON_DOCS in both host and worker environments for a custom root.

    This never downloads or writes. Native permissions determine write access;
    unrestricted execution and explicit writable grants do not ensure read-only
    documentation. Paths belong to this worker host, not necessarily its client.
    """
    import json

    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    destination = documentation_root() / minor
    manifest = destination / "manifest.json"
    if not manifest.exists():
        return None
    receipt = json.loads(manifest.read_text(encoding="utf-8"))
    if receipt["python_minor"] != minor:
        raise ValueError(
            "Python documentation cache version does not match the active interpreter"
        )
    directory = destination / "manual"
    if not (directory / "contents.txt").is_file():
        raise ValueError(
            "Python documentation cache is incomplete; prepare it again on this host"
        )
    return {
        **receipt,
        "directory": directory,
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
    }
