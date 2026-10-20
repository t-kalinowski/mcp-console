# Connect to Console

Use Console with an MCP client, from an R chat, or from Python.
Each connects to the same persistent workbench for R, Python, and SQL.

## With an MCP client {#mcp-clients}

[Get started](getting-started.qmd#connect-your-client) covers setup for Codex, Claude Code, and other MCP clients.
Console uses MCP over standard input and output.
For a client with manual MCP settings, use command `uvx` and arguments `["mcp-console", "serve"]`.
See [Other MCP clients](CLI.md#other-mcp-clients) for a configuration example.

<span id="connect-from-application-code"></span>

## From R

The `mcp.console` package adds Console to an ellmer chat.
[Use Console from R](R.md) shows installation, tool registration, and session configuration.
The [R API](https://t-kalinowski.github.io/mcp-console/r/reference/index.html) documents exact signatures.

## From Python

The `mcp_console` package provides synchronous and asynchronous clients and adapters for model frameworks.
[Use Console from Python](PYTHON.md) covers direct calls, adapters, results, and connection lifetime.
The [Python API](https://t-kalinowski.github.io/mcp-console/python/reference/index.html) documents exact signatures and complete examples.

## Learn and customize Console

<span id="configure-the-models-interface"></span>

The [user guides](README.md#learn-console) cover sessions, packages, sandbox permissions, and recordings for all three connection methods.
[Customize Console](BUILD_ON_CONSOLE.md) shows how to change the tool descriptions, exposed languages and parameters, and prepared environment.
[Tool configuration](TOOL_CONFIGURATION.md) documents the exact settings.

## Use the MCP tool

[`send` reference](API.md) documents arguments, results, and output limits.
[Calls, input, and control](SEND_OPERATIONS.md) covers sequencing, polling, interactive input, interruption, and restart.

[Requirements API](REQUIREMENTS_API.md) documents dependency actions and their effects on a running session.
[Runtime reference](RUNTIME_REFERENCE.md) provides detailed language behavior and helper contracts.

## Developing Console itself

The relay and worker protocols are implementation details, not alternative MCP interfaces.
They are documented under [Development](DEVELOPMENT.md).
