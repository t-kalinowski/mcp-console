# Use Console with an MCP client

Connect Console to your coding agent or another client that supports MCP over standard input and output.
[Get started](getting-started.qmd) walks through installation, connection, and a first analysis.
This page covers registration scope and client setup options; the commands below require [uv](https://docs.astral.sh/uv/getting-started/installation/).

<span id="mcp-clients"></span>

## Codex and Claude Code

Run the command for your client:

```sh
uvx mcp-console configure-client codex
uvx mcp-console configure-client claude
```

This registers Console globally, so it is available across projects, and loads its tools up front.
It also adds Console's `send` tool to the client's allowlist so calls do not need individual approval, subject to explicit client restrictions.
Setup starts runtime checks and cache warming in the background, printing a log path for the results.
Use `--no-warmup` to skip that step.
Restart the client after setup.
Your client launches Console on the same host, usually in the workspace where the agent is working.

To register Console for a specific project, run the command from the project directory and add `--directory .`.
See [client configuration](CLI.md#configure-a-client) for project registration, version pinning, and launch options.

## Other MCP clients

In your client's MCP settings, use command `uvx` and arguments `["mcp-console", "serve"]`.
See [Other MCP clients](CLI.md#other-mcp-clients) for a configuration example.

## R and Python clients {#connect-from-application-code}

<span id="from-r"></span> <span id="from-python"></span>

To use Console from R or Python, follow [Use Console from R](R.md) or [Use Console from Python](PYTHON.md).
These clients use the same workbench and settings.

<span id="configure-the-models-interface"></span>

[Customize Console](BUILD_ON_CONSOLE.md) shows how to change the tool descriptions, exposed languages and parameters, and prepared environment.
[Tool configuration](TOOL_CONFIGURATION.md) documents the exact settings.

After connecting, [Sessions and languages](BUILTIN_RUNTIME.md) explains how your agent can work with Console.
