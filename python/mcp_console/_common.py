from __future__ import annotations

import json
import os
import shutil
import sysconfig
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from mcp.types import CallToolResult, ContentBlock

Command = str | os.PathLike[str]


class Requirements(TypedDict, total=False):
    """Dependency declarations accepted by the clients' `send(requirements=...)`.

    `add` merges packages into the session declaration. `get` inspects it, `set`
    replaces it (emptying omitted fields), and `reset` restores startup defaults.
    Changed replacements of a live worker require `control="restart"`, which
    also discards its variables. Python packages run in Console's worker
    environment; installing a framework extra prepares the calling application.

    Attributes:
        action: `get`, `add` (the default), `set`, or `reset`.
        python_version: Python version constraints, such as `[">=3.11"]`.
        exclude_newer: Optional cutoff date for dependency resolution.
        r: R package requirements.
        python: Python package requirements.
        duckdb: DuckDB extensions.

    Examples:
        Install with `pip install 'mcp-console[client]'`. Add NumPy to Console's
        managed Python environment and use it in the same call:

        ```python
        from mcp_console import MCPConsole, Requirements

        requirements: Requirements = {"python": ["numpy"]}
        with MCPConsole() as console:
            print(
                console.send(
                    requirements=requirements,
                    python="import numpy as np; np.mean([12, 15, 18, 20, 25])",
                )
            )
        ```
    """

    action: Literal["get", "add", "set", "reset"]
    python_version: list[str]
    exclude_newer: str | None
    r: list[str]
    python: list[str]
    duckdb: list[str]


def stdio_command(
    command: Command | None, args: Sequence[Command] | None
) -> tuple[str, list[str]]:
    if command is None:
        candidate = Path(sysconfig.get_path("scripts")) / "mcp-console"
        command = (
            candidate if os.access(candidate, os.X_OK) else shutil.which("mcp-console")
        )
        if command is None:
            raise FileNotFoundError(
                "Install mcp-console in this Python environment or pass command= explicitly"
            )
    return os.fspath(command), ["serve"] if args is None else [
        os.fspath(arg) for arg in args
    ]


def content_text(content: ContentBlock) -> str:
    if content.type == "text":
        return content.text
    if content.type == "image":
        return f"[{content.mime_type} output]"
    return content.model_dump_json(by_alias=True, exclude_none=True)


def result_text(result: CallToolResult) -> str:
    text = ""
    for item in result.content:
        part = content_text(item)
        if text and part and not text.endswith("\n") and not part.startswith("\n"):
            text += "\n"
        text += part
    if result.is_error:
        raise RuntimeError(text or "MCP Console returned an error")
    if (
        isinstance(result.structured_content, dict)
        and "requirements" in result.structured_content
    ):
        return json.dumps(result.structured_content, indent=2)
    return text
