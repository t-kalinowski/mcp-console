from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self, cast

from annotated_types import Ge, Le, MaxLen, MinLen
from typing_extensions import TypeAliasType, TypedDict

from ._common import Command, configured_send, result_text, stdio_command
from .openai import AsyncResponsesTool

if TYPE_CHECKING:
    from mcp import Client
    from mcp.types import CallToolResult, Tool


# A named alias keeps Agents and chatlas from discarding the constraints.
TimeoutMilliseconds = TypeAliasType(
    "TimeoutMilliseconds", Annotated[int, Ge(0), Le(2**64 - 1)]
)


class Requirements(TypedDict, total=False, closed=True):
    r: Annotated[list[Annotated[str, MinLen(1)]], MaxLen(64)]
    python: Annotated[list[Annotated[str, MinLen(1)]], MaxLen(64)]
    duckdb: Annotated[list[Annotated[str, MinLen(1), MaxLen(64)]], MaxLen(64)]


def _nonempty_requirements_schema(schema: dict[str, Any]) -> None:
    properties = schema["properties"]
    # Keep all keys in each branch for SDKs that close objects in strict mode.
    schema["anyOf"] = [
        schema
        | {
            "properties": properties | {name: field | {"minItems": 1}},
            "required": [name],
        }
        for name, field in properties.items()
    ]


# Configure SDK schema generation without importing their Pydantic dependency.
cast(Any, Requirements).__pydantic_config__ = {
    "json_schema_extra": _nonempty_requirements_schema
}


class AsyncMCPConsole:
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

    async def connect(self) -> "Self":
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
            send = configured_send(self.send, send_tool)
            self._stack = stack.pop_all()
            self._client = client
            self._send_tool = send_tool
            self.send = send
        return self

    async def close(self) -> None:
        """Close the MCP client session and terminate its subprocess."""
        stack = self._stack
        self._stack = self._client = self._send_tool = None
        self.__dict__.pop("send", None)
        if stack is not None:
            await stack.aclose()

    async def __aenter__(self) -> "Self":
        return await self.connect()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    async def _call_send(self, arguments: Mapping[str, Any]) -> "CallToolResult":
        if self._client is None:
            raise RuntimeError(
                "AsyncMCPConsole is not connected; use `async with AsyncMCPConsole() as console` "
                "or call `await console.connect()` first"
            )
        return await self._client.call_tool("send", dict(arguments))

    # Framework schema generators need concrete callable annotations.
    async def send(
        self,
        *,
        r: str | None = None,
        python: str | None = None,
        sql: str | None = None,
        control: Literal["interrupt", "restart"] | None = None,
        requirements: Requirements | None = None,
        stdin: str | None = None,
        timeout_ms: TimeoutMilliseconds = 60_000,
    ) -> str:
        """Run or control the persistent R, Python, and SQL console.

        Send at most one of r, python, or sql per call.
        If output ends in ``[running; poll with an empty send]``, call again
        without code or stdin until completion before submitting another cell.

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

    def _connected_tool(self) -> "Tool":
        if self._send_tool is None:
            raise RuntimeError(
                "Connect the console before creating tools; enter its context or call connect()"
            )
        return self._send_tool

    def openai_responses_tool(self) -> "AsyncResponsesTool":
        """Return an object for a standard OpenAI Responses tool loop."""
        return AsyncResponsesTool(self, self._connected_tool())

    def openai_agents_tool(self, *, strict_mode: bool = False, **kwargs: Any) -> Any:
        """Return a native OpenAI Agents function tool wrapping ``send``."""
        self._connected_tool()
        from agents import function_tool

        # send accepts sparse arguments and requirements rather than a strict object.
        tool = function_tool(self.send, strict_mode=strict_mode, **kwargs)
        tool.params_json_schema["additionalProperties"] = False
        return tool

    def anthropic_tool(self, **kwargs: Any) -> Any:
        """Return a native Anthropic async function tool wrapping ``send``."""
        self._connected_tool()
        from anthropic import beta_async_tool

        return beta_async_tool(self.send, **kwargs)
