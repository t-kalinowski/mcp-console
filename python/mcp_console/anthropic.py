"""Native Anthropic tools backed by MCP Console."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Any

from ._common import Command, stdio_command

if TYPE_CHECKING:
    from ._client import AsyncMCPConsole
    from ._sync import MCPConsole


def tool(console: MCPConsole | AsyncMCPConsole, **kwargs: Any) -> Any:
    """Return a sync or async runner tool using the live MCP schema."""
    from anthropic import beta_async_tool, beta_tool

    definition = console.send_tool
    if iscoroutinefunction(console.send):

        async def invoke(**arguments: Any) -> str:
            return await console.send(**arguments)

        decorate = beta_async_tool
    else:

        def invoke(**arguments: Any) -> str:
            return console.send(**arguments)

        decorate = beta_tool

    return decorate(
        invoke,
        name=definition.name,
        description=definition.description,
        input_schema=definition.input_schema,
        **kwargs,
    )


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
