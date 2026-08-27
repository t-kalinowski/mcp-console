"""Framework adapters for the MCP Console stdio server."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sysconfig
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import (
    Any,
    AsyncIterator,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

__all__ = [
    "anthropic_tools",
    "openai_agents_server",
    "openai_codex",
    "register_chatlas",
]

Command = Union[str, os.PathLike]


def _executable() -> str:
    executable_name = "mcp-console.exe" if os.name == "nt" else "mcp-console"
    scripts = sysconfig.get_path("scripts")
    if scripts is not None:
        candidate = Path(scripts) / executable_name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    candidate = shutil.which("mcp-console")
    if candidate is not None:
        return candidate

    raise FileNotFoundError(
        "Could not find the mcp-console executable. Install mcp-console into "
        "this Python environment, or pass command= and args= explicitly."
    )


def _stdio_command(
    command: Optional[Command], args: Optional[Sequence[Command]]
) -> Tuple[str, List[str]]:
    resolved_command = _executable() if command is None else os.fspath(command)
    resolved_args = ["serve"] if args is None else [os.fspath(arg) for arg in args]
    return resolved_command, resolved_args


async def register_chatlas(
    chat: Any,
    *,
    command: Optional[Command] = None,
    args: Optional[Sequence[Command]] = None,
    **kwargs: Any,
) -> Any:
    """Launch MCP Console and register its tools on a chatlas ``Chat``.

    Remaining keyword arguments are forwarded to
    ``Chat.register_mcp_tools_stdio_async()``. The chat owns the connection;
    call ``await chat.cleanup_mcp_tools()`` when it is no longer needed.
    """
    resolved_command, resolved_args = _stdio_command(command, args)
    return await chat.register_mcp_tools_stdio_async(
        command=resolved_command,
        args=resolved_args,
        **kwargs,
    )


def openai_agents_server(
    *,
    command: Optional[Command] = None,
    args: Optional[Sequence[Command]] = None,
    name: str = "MCP Console",
    params: Optional[Mapping[str, Any]] = None,
    **kwargs: Any,
) -> Any:
    """Return an OpenAI Agents SDK ``MCPServerStdio`` for MCP Console.

    ``params`` may contain additional native stdio process settings. Remaining
    keyword arguments are forwarded to ``MCPServerStdio``.
    """
    try:
        from agents.mcp import MCPServerStdio
    except ImportError as error:
        raise ImportError(
            "The OpenAI integration requires openai-agents. Install it with "
            "`pip install 'mcp-console[openai]'`."
        ) from error

    resolved_command, resolved_args = _stdio_command(command, args)
    server_params: Dict[str, Any] = dict(params or {})
    server_params.update(command=resolved_command, args=resolved_args)
    return MCPServerStdio(name=name, params=server_params, **kwargs)


@contextmanager
def openai_codex(
    *,
    command: Optional[Command] = None,
    args: Optional[Sequence[Command]] = None,
    server_name: str = "mcp-console",
    options: Optional[Mapping[str, Any]] = None,
) -> Iterator[Any]:
    """Yield an ``openai_codex_sdk.Codex`` configured with MCP Console.

    The helper preserves the SDK's native options and Codex configuration. It
    temporarily replaces only ``codex_path_override`` with a launcher that adds
    MCP server command and argument overrides before delegating to the resolved
    Codex executable.
    """
    try:
        from openai_codex_sdk import Codex
        from openai_codex_sdk.exec import find_codex_path
    except ImportError as error:
        raise ImportError(
            "The Codex integration requires openai-codex-sdk. Install it with "
            "`pip install 'mcp-console[codex]'`."
        ) from error

    resolved_command, resolved_args = _stdio_command(command, args)
    codex_options: Dict[str, Any] = dict(options or {})
    snake_path = codex_options.pop("codex_path_override", None)
    camel_path = codex_options.pop("codexPathOverride", None)
    if snake_path is not None and camel_path is not None:
        raise ValueError(
            "options cannot contain both codex_path_override and codexPathOverride"
        )
    codex_path = snake_path if snake_path is not None else camel_path
    if codex_path is None:
        codex_path = find_codex_path()

    table = f"mcp_servers.{json.dumps(server_name)}"
    overrides = [
        f"{table}.command={json.dumps(resolved_command)}",
        f"{table}.args={json.dumps(resolved_args)}",
    ]

    with TemporaryDirectory(prefix="mcp-console-codex-") as directory:
        launcher = Path(directory) / "codex"
        delegated = shlex.quote(os.fspath(codex_path))
        config_arguments = " ".join(
            part
            for override in overrides
            for part in ("--config", shlex.quote(override))
        )
        launcher.write_text(
            "#!/bin/sh\n"
            'if [ "$#" -gt 0 ] && [ "$1" = "exec" ]; then\n'
            "  shift\n"
            f'  exec {delegated} exec {config_arguments} "$@"\n'
            "fi\n"
            f'exec {delegated} "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        codex_options["codex_path_override"] = str(launcher)
        yield Codex(codex_options)


@asynccontextmanager
async def anthropic_tools(
    *,
    command: Optional[Command] = None,
    args: Optional[Sequence[Command]] = None,
    server_parameters: Optional[Mapping[str, Any]] = None,
    tool_kwargs: Optional[Mapping[str, Any]] = None,
) -> AsyncIterator[List[Any]]:
    """Yield MCP Console tools for Anthropic's async ``tool_runner()``.

    The context manager owns the MCP subprocess and client session. Keep the
    Anthropic tool runner inside the context so its tools retain that session.
    """
    try:
        from anthropic.lib.tools.mcp import async_mcp_tool
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError as error:
        raise ImportError(
            "The Anthropic integration requires anthropic[mcp]. Install it "
            "with `pip install 'mcp-console[anthropic]'`."
        ) from error

    resolved_command, resolved_args = _stdio_command(command, args)
    parameters: Dict[str, Any] = dict(server_parameters or {})
    parameters.update(command=resolved_command, args=resolved_args)
    options = dict(tool_kwargs or {})

    async with stdio_client(StdioServerParameters(**parameters)) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
            yield [async_mcp_tool(tool, client, **options) for tool in tools.tools]
