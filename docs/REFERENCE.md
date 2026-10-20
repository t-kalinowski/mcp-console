# Reference

Look up Console's settings, commands, and APIs by the task you want to accomplish.
For an introduction to sessions, packages, permissions, and recordings, start with the [guides](README.md).

## Find a task

| I want to…                                                                                        | Reference                                                                                  |
| ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Customize the agent's tools and prepare its environment                                           | [Customize Console](BUILD_ON_CONSOLE.md)                                                   |
| Create a configuration file, understand which settings win, or inspect the resolved configuration | [Configuration files](CONFIGURATION.md)                                                    |
| Use a Python environment or select an R installation                                              | [Interpreters and startup](RUNTIME_CONFIGURATION.md)                                       |
| Choose startup packages or control when more may be prepared                                      | [Package declarations and policies](RUNTIME_CONFIGURATION.md#startup-package-declarations) |
| Connect to a database before the first SQL call                                                   | [Database startup](RUNTIME_CONFIGURATION.md#session-startup-source)                        |
| Choose what languages, parameters, or instructions the agent sees                                 | [Tool configuration](TOOL_CONFIGURATION.md)                                                |
| Allow project writes, protect files, reach a service, or inherit Codex permissions                | [Sandbox permissions](SANDBOX_CONFIGURATION.md)                                            |
| Use a package mirror or change cache locations                                                    | [Package sources and caches](RESOLVER.md)                                                  |
| Set environment variables or look up how Console handles them                                     | [Environment variables](ENVIRONMENT.md)                                                    |
| Look up language helpers, SQL connections, plotting, or output behavior                           | [Runtime behavior](RUNTIME_REFERENCE.md)                                                   |
| Find recorded output or turn an analysis into a report                                            | [Recordings and reports](RECORDING.md)                                                     |
| Run a program with filesystem and network limits                                                  | [Sandbox command](SANDBOX_COMMAND.md)                                                      |
| Register a client, check an installation, or look up a command                                    | [Command-line reference](CLI.md) and [CLI help](cli-help.qmd)                              |

For a particular configuration key, use the [settings index](CONFIGURATION.md#settings-by-task).

## APIs and package manuals

- [MCP send reference](API.md) documents tool arguments, results, and output limits.
  [Calls, input, and control](SEND_OPERATIONS.md) covers polling, interactive input, interruption, and restart; [Requirements API](REQUIREMENTS_API.md) covers dependency actions.
- The [R API](https://t-kalinowski.github.io/mcp-console/r/reference/index.html) documents tool registration and session configuration.
  The [R package manual](../r/README.md) includes configuration and analysis examples.
- The [Python API](https://t-kalinowski.github.io/mcp-console/python/reference/index.html) documents synchronous and asynchronous clients and model integrations.
  The [Python package manual](https://t-kalinowski.github.io/mcp-console/python/index.html) introduces the available clients and adapters.
