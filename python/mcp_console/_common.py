from __future__ import annotations

import os
import shutil
import sysconfig
from collections.abc import Callable, Sequence
from functools import wraps
from inspect import iscoroutinefunction, signature
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.types import CallToolResult, ContentBlock, Tool

Command = str | os.PathLike[str]


def configured_send(send: Callable[..., Any], tool: Tool) -> Any:
    # Copy the callable so each connection advertises only its server's fields.
    if iscoroutinefunction(send):

        @wraps(send)
        async def configured(**kwargs):
            return await send(**kwargs)
    else:

        @wraps(send)
        def configured(**kwargs):
            return send(**kwargs)

    original = signature(send)
    properties = tool.input_schema["properties"]
    parameters = [p for name, p in original.parameters.items() if name in properties]
    configured.__signature__ = original.replace(parameters=parameters)
    omitted = original.parameters.keys() - properties.keys()
    configured.__doc__ = "\n".join(
        line
        for line in send.__doc__.splitlines()
        if line.strip().partition(":")[0] not in omitted
    )
    return configured


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
    return text
