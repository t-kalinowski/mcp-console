"""Register MCP Console on an existing chatlas chat."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ._common import Command, stdio_command

if TYPE_CHECKING:
    from chatlas import Tool

    from ._client import AsyncMCPConsole
    from ._sync import MCPConsole


def tool(console: MCPConsole | AsyncMCPConsole) -> Tool:
    """Return a tool for ``chat.set_tools()`` using the live MCP schema."""
    from chatlas import Tool

    definition = console.send_tool
    return Tool(
        func=console.send,
        name=definition.name,
        description=definition.description or "",
        parameters=definition.input_schema,
        strict=False,
    )


async def register(
    chat: Any,
    *,
    command: Command | None = None,
    args: Sequence[Command] | None = None,
    **kwargs: Any,
) -> Any:
    """Register MCP Console on an existing chatlas ``Chat``."""
    command, args = stdio_command(command, args)
    return await chat.register_mcp_tools_stdio_async(
        command=command, args=args, **kwargs
    )
