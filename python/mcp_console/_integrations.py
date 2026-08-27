from __future__ import annotations

import json
import os
import shlex
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
)

from ._common import Command, stdio_command


async def register_chatlas(
    chat: Any,
    *,
    command: Optional[Command] = None,
    args: Optional[Sequence[Command]] = None,
    **kwargs: Any,
) -> Any:
    """Register MCP Console on an existing chatlas ``Chat``."""
    resolved_command, resolved_args = stdio_command(command, args)
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
    """Return the native OpenAI Agents ``MCPServerStdio`` object."""
    try:
        from agents.mcp import MCPServerStdio
    except ImportError as error:
        raise ImportError(
            "The OpenAI Agents integration requires openai-agents. Install it "
            "with `pip install 'mcp-console[openai-agents]'`."
        ) from error

    resolved_command, resolved_args = stdio_command(command, args)
    server_params: Dict[str, Any] = dict(params or {})
    server_params.update(command=resolved_command, args=resolved_args)
    return MCPServerStdio(name=name, params=server_params, **kwargs)


@asynccontextmanager
async def anthropic_tools(
    *,
    command: Optional[Command] = None,
    args: Optional[Sequence[Command]] = None,
    server_parameters: Optional[Mapping[str, Any]] = None,
    tool_kwargs: Optional[Mapping[str, Any]] = None,
) -> AsyncIterator[List[Any]]:
    """Yield native Anthropic tools backed by a live MCP Console session."""
    try:
        from anthropic.lib.tools.mcp import async_mcp_tool
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError as error:
        raise ImportError(
            "The Anthropic integration requires anthropic[mcp]. Install it "
            "with `pip install 'mcp-console[anthropic]'`."
        ) from error

    resolved_command, resolved_args = stdio_command(command, args)
    parameters: Dict[str, Any] = dict(server_parameters or {})
    parameters.update(command=resolved_command, args=resolved_args)
    options = dict(tool_kwargs or {})

    async with stdio_client(StdioServerParameters(**parameters)) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
            yield [async_mcp_tool(tool, client, **options) for tool in tools.tools]


def codex_config(
    *,
    command: Optional[Command] = None,
    args: Optional[Sequence[Command]] = None,
    server_name: str = "mcp-console",
    config: Optional[Mapping[str, Any]] = None,
    server_parameters: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return config for ``openai_codex.Codex.thread_start(config=...)``.

    The returned mapping is ordinary Codex thread configuration. MCP Console
    creates no Codex client or thread.
    """
    resolved_command, resolved_args = stdio_command(command, args)
    result: Dict[str, Any] = dict(config or {})
    servers = dict(result.get("mcp_servers", {}))
    server = dict(server_parameters or {})
    server.update(command=resolved_command, args=resolved_args)
    servers[server_name] = server
    result["mcp_servers"] = servers
    return result


@contextmanager
def openai_codex_sdk_options(
    *,
    command: Optional[Command] = None,
    args: Optional[Sequence[Command]] = None,
    server_name: str = "mcp-console",
    options: Optional[Mapping[str, Any]] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield constructor options for the legacy ``openai-codex-sdk`` package.

    Version 0.1.11 of that package exposes neither per-thread tools nor generic
    Codex config. This compatibility shim therefore supplies a temporary Codex
    executable override. The caller still constructs and owns ``Codex`` and all
    threads.
    """
    try:
        from openai_codex_sdk.exec import find_codex_path
    except ImportError as error:
        raise ImportError(
            "This compatibility integration requires openai-codex-sdk. Install "
            "it with `pip install 'mcp-console[codex-sdk]'`."
        ) from error

    resolved_command, resolved_args = stdio_command(command, args)
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
        yield codex_options
