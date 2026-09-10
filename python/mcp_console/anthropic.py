"""Native Anthropic tools backed by MCP Console."""

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from ._common import Command, stdio_command


@asynccontextmanager
async def tools(
    *,
    command: Command | None = None,
    args: Sequence[Command] | None = None,
    server_parameters: Mapping[str, Any] | None = None,
    tool_kwargs: Mapping[str, Any] | None = None,
) -> AsyncIterator[list[Any]]:
    """Yield native Anthropic tools backed by a live MCP Console session."""
    from anthropic.lib.tools.mcp import async_mcp_tool
    from mcp import Client, StdioServerParameters

    command, args = stdio_command(command, args)
    parameters = dict(server_parameters or {}) | {"command": command, "args": args}
    async with Client(StdioServerParameters(**parameters)) as client:
        tools = await client.list_tools()
        yield [
            async_mcp_tool(tool, client, **dict(tool_kwargs or {}))
            for tool in tools.tools
        ]
