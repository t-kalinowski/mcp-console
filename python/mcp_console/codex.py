"""MCP Console configuration for the official thread SDK."""

from collections.abc import Mapping, Sequence
from typing import Any

from ._common import Command, stdio_command


def server(
    *,
    command: Command | None = None,
    args: Sequence[Command] | None = None,
    server_parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one stdio server entry for the thread SDK's ``mcp_servers`` config."""
    command, args = stdio_command(command, args)
    return dict(server_parameters or {}) | {"command": command, "args": args}
