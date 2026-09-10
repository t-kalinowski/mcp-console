from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from ._common import Command, stdio_command


async def register_chatlas(
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


def openai_agents_server(
    *,
    command: Command | None = None,
    args: Sequence[Command] | None = None,
    name: str = "MCP Console",
    params: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Return the native OpenAI Agents ``MCPServerStdio`` object."""
    from agents.mcp import MCPServerStdio

    command, args = stdio_command(command, args)
    return MCPServerStdio(
        name=name,
        params=dict(params or {}) | {"command": command, "args": args},
        **kwargs,
    )


@asynccontextmanager
async def anthropic_tools(
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


def codex_config(
    *,
    command: Command | None = None,
    args: Sequence[Command] | None = None,
    server_name: str = "mcp-console",
    config: Mapping[str, Any] | None = None,
    server_parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return configuration for ``openai_codex.Codex.thread_start(config=...)``."""
    command, args = stdio_command(command, args)
    result = dict(config or {})
    server = dict(server_parameters or {}) | {"command": command, "args": args}
    result["mcp_servers"] = dict(result.get("mcp_servers", {})) | {server_name: server}
    return result
