from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, Self

from ._client import (
    AsyncMCPConsole,
    AsyncOpenAIResponsesTool,
    Requirements,
    TimeoutMilliseconds,
)
from ._common import Command

if TYPE_CHECKING:
    from anyio.from_thread import BlockingPortal


class MCPConsole:
    """A synchronous, persistent connection to ``mcp-console serve``.

    Use as a context manager or explicitly call ``connect()`` and ``close()``.
    The application owns its model clients, agents, and tool loops.
    """

    def __init__(
        self,
        *,
        command: Command | None = None,
        args: Sequence[Command] | None = None,
        server_parameters: Mapping[str, Any] | None = None,
    ) -> None:
        self._async = AsyncMCPConsole(
            command=command, args=args, server_parameters=server_parameters
        )
        self._stack: ExitStack | None = None
        self._portal: BlockingPortal | None = None

    def connect(self) -> "Self":
        """Start MCP Console and initialize its MCP client session."""
        if self._stack is None:
            from anyio.from_thread import start_blocking_portal

            with ExitStack() as stack:
                portal = stack.enter_context(start_blocking_portal())
                # The MCP transport must enter and exit in the same async task.
                stack.enter_context(portal.wrap_async_context_manager(self._async))
                self._portal = portal
                self._stack = stack.pop_all()
        return self

    def close(self) -> None:
        """Close the MCP session, subprocess, and portal thread."""
        stack = self._stack
        self._stack = self._portal = None
        if stack is not None:
            stack.close()

    def __enter__(self) -> "Self":
        return self.connect()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _run(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._portal is None:
            raise RuntimeError(
                "MCPConsole is not connected; use `with MCPConsole() as console` "
                "or call `console.connect()` first"
            )
        return self._portal.call(partial(func, *args, **kwargs))

    def send(
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
        return self._run(
            self._async.send,
            r=r,
            python=python,
            sql=sql,
            control=control,
            requirements=requirements,
            stdin=stdin,
            timeout_ms=timeout_ms,
        )

    send.__doc__ = AsyncMCPConsole.send.__doc__
    __call__ = send

    def openai_responses_tool(self) -> "OpenAIResponsesTool":
        """Return a synchronous tool for an OpenAI Responses tool loop."""
        return OpenAIResponsesTool(self, self._run(self._async.openai_responses_tool))

    def openai_agents_tool(self, *, strict_mode: bool = False, **kwargs: Any) -> Any:
        """Return a native OpenAI Agents function tool wrapping ``send``."""
        from agents import function_tool

        return function_tool(self.send, strict_mode=strict_mode, **kwargs)

    def anthropic_tool(self, **kwargs: Any) -> Any:
        """Return a native Anthropic synchronous function tool wrapping ``send``."""
        from anthropic import beta_tool

        return beta_tool(self.send, **kwargs)


class OpenAIResponsesTool:
    """A synchronous function tool for the OpenAI Responses API."""

    def __init__(self, console: MCPConsole, tool: AsyncOpenAIResponsesTool) -> None:
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
