from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import (
    Any,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from ._common import (
    Command,
    field,
    openai_result_output,
    result_text,
    stdio_command,
)

Requirements = Dict[str, List[str]]


class MCPConsole:
    """A persistent, callable connection to ``mcp-console serve``.

    This object owns only the local MCP subprocess and client session. It does
    not create or own a chat, agent, model client, Codex thread, or tool loop.
    """

    def __init__(
        self,
        *,
        command: Optional[Command] = None,
        args: Optional[Sequence[Command]] = None,
        server_parameters: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._command = command
        self._args = args
        self._server_parameters = dict(server_parameters or {})
        self._stack: Optional[AsyncExitStack] = None
        self._client: Any = None
        self._send_tool: Any = None

    async def connect(self) -> "MCPConsole":
        """Start MCP Console and initialize its MCP client session."""
        if self._client is not None:
            return self

        try:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client
        except ImportError as error:
            raise ImportError(
                "The callable MCP Console client requires the mcp package. "
                "Install it with `pip install 'mcp-console[client]'`."
            ) from error

        resolved_command, resolved_args = stdio_command(self._command, self._args)
        parameters: Dict[str, Any] = dict(self._server_parameters)
        parameters.update(command=resolved_command, args=resolved_args)

        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(
                stdio_client(StdioServerParameters(**parameters))
            )
            client = await stack.enter_async_context(ClientSession(read, write))
            await client.initialize()
            tools = await client.list_tools()
            send_tool = next(
                (tool for tool in tools.tools if field(tool, "name", "name") == "send"),
                None,
            )
            if send_tool is None:
                raise RuntimeError("MCP Console did not expose its send tool")
        except BaseException:
            await stack.aclose()
            raise

        self._stack = stack
        self._client = client
        self._send_tool = send_tool
        return self

    async def close(self) -> None:
        """Close the MCP client session and terminate its subprocess."""
        stack = self._stack
        self._stack = None
        self._client = None
        self._send_tool = None
        if stack is not None:
            await stack.aclose()

    async def __aenter__(self) -> "MCPConsole":
        return await self.connect()

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    def _arguments(
        self,
        *,
        r: Optional[str],
        python: Optional[str],
        sql: Optional[str],
        control: Optional[Literal["interrupt", "restart"]],
        requirements: Optional[Requirements],
        stdin: Optional[str],
        timeout_ms: int,
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"timeout_ms": timeout_ms}
        for name, value in (
            ("r", r),
            ("python", python),
            ("sql", sql),
            ("control", control),
            ("requirements", requirements),
            ("stdin", stdin),
        ):
            if value is not None:
                arguments[name] = value
        return arguments

    async def _call_send(self, arguments: Mapping[str, Any]) -> Any:
        if self._client is None:
            raise RuntimeError(
                "MCPConsole is not connected; use `async with MCPConsole() as console` "
                "or call `await console.connect()` first"
            )
        return await self._client.call_tool("send", dict(arguments))

    async def send(
        self,
        *,
        r: Optional[str] = None,
        python: Optional[str] = None,
        sql: Optional[str] = None,
        control: Optional[Literal["interrupt", "restart"]] = None,
        requirements: Optional[Requirements] = None,
        stdin: Optional[str] = None,
        timeout_ms: int = 60_000,
    ) -> str:
        """Run or control the persistent R, Python, and DuckDB console.

        Args:
            r: One complete R cell.
            python: One complete Python cell.
            sql: One complete DuckDB SQL cell.
            control: Interrupt the active operation or restart the runtime.
            requirements: Additive R, Python, or DuckDB requirements.
            stdin: Exact input for an active prompt or debugger.
            timeout_ms: Maximum wait after dispatch; timeout does not cancel work.
        """
        arguments = self._arguments(
            r=r,
            python=python,
            sql=sql,
            control=control,
            requirements=requirements,
            stdin=stdin,
            timeout_ms=timeout_ms,
        )
        return result_text(await self._call_send(arguments))

    __call__ = send

    def openai_responses_tool(self) -> "OpenAIResponsesTool":
        """Return an object for a standard OpenAI Responses tool loop."""
        if self._send_tool is None:
            raise RuntimeError(
                "MCPConsole must be connected before creating an OpenAI tool"
            )
        return OpenAIResponsesTool(self, self._send_tool)

    def openai_agents_tool(self, **kwargs: Any) -> Any:
        """Return a native OpenAI Agents ``FunctionTool`` wrapping ``send``."""
        try:
            from agents import function_tool
        except ImportError as error:
            raise ImportError(
                "The OpenAI Agents integration requires openai-agents. Install "
                "it with `pip install 'mcp-console[openai-agents]'`."
            ) from error
        return function_tool(self.send, **kwargs)

    def anthropic_tool(self, **kwargs: Any) -> Any:
        """Return a native Anthropic async function tool wrapping ``send``."""
        try:
            from anthropic import beta_async_tool
        except ImportError as error:
            raise ImportError(
                "The Anthropic integration requires anthropic. Install it with "
                "`pip install 'mcp-console[anthropic]'`."
            ) from error
        return beta_async_tool(self.send, **kwargs)

    async def register_chatlas(self, chat: Any, **kwargs: Any) -> Any:
        """Register the native stdio MCP server on an existing chatlas Chat."""
        from ._integrations import register_chatlas

        return await register_chatlas(
            chat,
            command=self._command,
            args=self._args,
            **kwargs,
        )

    def openai_agents_server(self, **kwargs: Any) -> Any:
        """Return the native OpenAI Agents MCP server object."""
        from ._integrations import openai_agents_server

        return openai_agents_server(
            command=self._command,
            args=self._args,
            **kwargs,
        )

    def anthropic_tools(self, **kwargs: Any) -> Any:
        """Return the native Anthropic MCP tool context manager."""
        from ._integrations import anthropic_tools

        return anthropic_tools(
            command=self._command,
            args=self._args,
            **kwargs,
        )

    def codex_config(
        self,
        *,
        server_name: str = "mcp-console",
        config: Optional[Mapping[str, Any]] = None,
        server_parameters: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Return Codex thread configuration containing MCP Console."""
        from ._integrations import codex_config

        return codex_config(
            command=self._command,
            args=self._args,
            server_name=server_name,
            config=config,
            server_parameters=server_parameters,
        )

    def openai_codex_sdk_options(
        self,
        *,
        server_name: str = "mcp-console",
        options: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Return the legacy openai-codex-sdk options context manager."""
        from ._integrations import openai_codex_sdk_options

        return openai_codex_sdk_options(
            command=self._command,
            args=self._args,
            server_name=server_name,
            options=options,
        )


class OpenAIResponsesTool:
    """MCP Console as one function tool for the OpenAI Responses API."""

    def __init__(self, console: MCPConsole, tool: Any) -> None:
        self._console = console
        self._tool = tool

    @property
    def definition(self) -> Dict[str, Any]:
        """Function-tool definition to place in ``responses.create(tools=...)``."""
        schema = field(self._tool, "input_schema", "inputSchema") or {
            "type": "object",
            "properties": {},
        }
        return {
            "type": "function",
            "name": field(self._tool, "name", "name"),
            "description": field(self._tool, "description", "description"),
            "parameters": schema,
            "strict": False,
        }

    async def call(self, arguments: Union[str, Mapping[str, Any]]) -> Any:
        """Execute one function call's JSON arguments and preserve rich output."""
        parsed: Any = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(parsed, Mapping):
            raise TypeError("OpenAI function arguments must decode to a JSON object")
        return openai_result_output(await self._console._call_send(parsed))

    async def output(self, call: Any) -> Dict[str, Any]:
        """Return one ``function_call_output`` item for an OpenAI call."""
        if isinstance(call, Mapping):
            call_id = call["call_id"]
            arguments = call["arguments"]
        else:
            call_id = call.call_id
            arguments = call.arguments
        return {
            "type": "function_call_output",
            "call_id": call_id,
            "output": await self.call(arguments),
        }

    __call__ = output
