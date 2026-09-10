"""Register MCP Console on an existing chatlas chat."""

from collections.abc import Sequence
from typing import Any

from ._common import Command, stdio_command


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
