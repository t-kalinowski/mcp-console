"""MCP Console adapters for OpenAI Responses and Agents."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ._common import Command, content_text, result_text, stdio_command

if TYPE_CHECKING:
    from mcp.types import Tool

    from ._client import AsyncMCPConsole
    from ._sync import MCPConsole


def agents_server(
    *,
    command: Command | None = None,
    args: Sequence[Command] | None = None,
    name: str = "MCP Console",
    params: Mapping[str, Any] | None = None,
    client_session_timeout_seconds: float | None = None,
    **kwargs: Any,
) -> Any:
    """Return the native OpenAI Agents ``MCPServerStdio`` object."""
    from agents.mcp import MCPServerStdio

    command, args = stdio_command(command, args)
    return MCPServerStdio(
        name=name,
        params=dict(params or {}) | {"command": command, "args": args},
        client_session_timeout_seconds=client_session_timeout_seconds,
        **kwargs,
    )


class AsyncResponsesTool:
    """MCP Console as one function tool for the OpenAI Responses API."""

    def __init__(self, console: AsyncMCPConsole, tool: Tool) -> None:
        self._console = console
        self._tool = tool

    @property
    def definition(self) -> dict[str, Any]:
        """Function-tool definition to place in ``responses.create(tools=...)``."""
        return {
            "type": "function",
            "name": self._tool.name,
            "description": self._tool.description,
            "parameters": self._tool.input_schema,
            "strict": False,
        }

    async def call(self, arguments: str | Mapping[str, Any]) -> str | list[dict]:
        """Execute one function call's JSON arguments and preserve rich output."""
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(parsed, Mapping):
            raise TypeError("OpenAI function arguments must decode to a JSON object")
        result = await self._console._call_send(parsed)
        text = result_text(result)
        if all(item.type == "text" for item in result.content):
            return text
        return [
            {
                "type": "input_image",
                "detail": "auto",
                "image_url": f"data:{item.mime_type};base64,{item.data}",
            }
            if item.type == "image"
            else {"type": "input_text", "text": content_text(item)}
            for item in result.content
        ]

    async def output(self, call: Any) -> dict[str, Any]:
        """Return one ``function_call_output`` item for an OpenAI call."""
        if isinstance(call, Mapping):
            call_id, arguments = call["call_id"], call["arguments"]
        else:
            call_id, arguments = call.call_id, call.arguments
        return {
            "type": "function_call_output",
            "call_id": call_id,
            "output": await self.call(arguments),
        }

    __call__ = output


class ResponsesTool:
    """A synchronous function tool for the OpenAI Responses API."""

    def __init__(self, console: MCPConsole, tool: AsyncResponsesTool) -> None:
        self._console = console
        self._async = tool

    @property
    def definition(self) -> dict[str, Any]:
        """Function-tool definition to place in ``responses.create(tools=...)``."""
        return self._async.definition

    def call(self, arguments: str | Mapping[str, Any]) -> str | list[dict]:
        """Execute one function call's JSON arguments and preserve rich output."""
        return self._console._run(self._async.call, arguments)

    def output(self, call: Any) -> dict[str, Any]:
        """Return one ``function_call_output`` item for an OpenAI call."""
        return self._console._run(self._async.output, call)

    __call__ = output
