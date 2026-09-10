from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, Literal

from ._common import Command, openai_result_output, result_text, stdio_command

if TYPE_CHECKING:
    from mcp import Client
    from mcp.types import CallToolResult, Tool
    from typing_extensions import Self

Requirements = dict[str, list[str]]


class MCPConsole:
    """A persistent, callable connection to ``mcp-console serve``.

    Enter and close the connection in the same async task. The application owns
    its model clients, agents, and tool loops.
    """

    def __init__(
        self,
        *,
        command: Command | None = None,
        args: Sequence[Command] | None = None,
        server_parameters: Mapping[str, Any] | None = None,
    ) -> None:
        self._command = command
        self._args = args
        self._server_parameters = dict(server_parameters or {})
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._send_tool: Tool | None = None

    async def connect(self) -> Self:
        """Start MCP Console and initialize its MCP client session."""
        if self._client is not None:
            return self

        from mcp import Client, StdioServerParameters

        command, args = stdio_command(self._command, self._args)
        parameters = self._server_parameters | {"command": command, "args": args}
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(
                Client(StdioServerParameters(**parameters))
            )
            tools = await client.list_tools()
            send_tool = next(
                (tool for tool in tools.tools if tool.name == "send"), None
            )
            if send_tool is None:
                raise RuntimeError("MCP Console did not expose its send tool")
            self._stack = stack.pop_all()
            self._client = client
            self._send_tool = send_tool
        return self

    async def close(self) -> None:
        """Close the MCP client session and terminate its subprocess."""
        stack = self._stack
        self._stack = self._client = self._send_tool = None
        if stack is not None:
            await stack.aclose()

    async def __aenter__(self) -> Self:
        return await self.connect()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    async def _call_send(self, arguments: Mapping[str, Any]) -> CallToolResult:
        if self._client is None:
            raise RuntimeError(
                "MCPConsole is not connected; use `async with MCPConsole() as console` "
                "or call `await console.connect()` first"
            )
        return await self._client.call_tool("send", dict(arguments))

    async def send(
        self,
        *,
        r: str | None = None,
        python: str | None = None,
        sql: str | None = None,
        control: Literal["interrupt", "restart"] | None = None,
        requirements: Requirements | None = None,
        stdin: str | None = None,
        timeout_ms: int = 60_000,
    ) -> str:
        """Run or control the persistent R, Python, and SQL console.

        Args:
            r: One complete R cell.
            python: One complete Python cell.
            sql: One complete SQL cell on the selected connection.
            control: Interrupt the active operation or restart the runtime.
            requirements: Additive requirements keyed by r, python, or duckdb.
            stdin: Exact input for an active prompt or debugger.
            timeout_ms: Maximum wait after dispatch; timeout does not cancel work.
        """
        arguments = {
            "r": r,
            "python": python,
            "sql": sql,
            "control": control,
            "requirements": requirements,
            "stdin": stdin,
            "timeout_ms": timeout_ms,
        }
        return result_text(
            await self._call_send(
                {name: value for name, value in arguments.items() if value is not None}
            )
        )

    __call__ = send

    def openai_responses_tool(self) -> OpenAIResponsesTool:
        """Return an object for a standard OpenAI Responses tool loop."""
        if self._send_tool is None:
            raise RuntimeError(
                "MCPConsole must be connected before creating an OpenAI tool"
            )
        return OpenAIResponsesTool(self, self._send_tool)

    def openai_agents_tool(self, *, strict_mode: bool = False, **kwargs: Any) -> Any:
        """Return a native OpenAI Agents function tool wrapping ``send``."""
        from agents import function_tool

        # send accepts sparse arguments and requirements rather than a strict object.
        return function_tool(self.send, strict_mode=strict_mode, **kwargs)

    def anthropic_tool(self, **kwargs: Any) -> Any:
        """Return a native Anthropic async function tool wrapping ``send``."""
        from anthropic import beta_async_tool

        return beta_async_tool(self.send, **kwargs)


class OpenAIResponsesTool:
    """MCP Console as one function tool for the OpenAI Responses API."""

    def __init__(self, console: MCPConsole, tool: Tool) -> None:
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
        return openai_result_output(await self._console._call_send(parsed))

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
