# Documentation

Choose a task below, or follow [Get started](getting-started.qmd) to connect a client and run a first analysis.
These pages apply whether you connect with an MCP client, from R, or from Python.

<span id="get-connected"></span> <span id="platform-setup"></span>

## Find a task

| I want to…                                                                                        | Start here                                                                                 |
| ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Connect a coding agent or another MCP client                                                      | [MCP clients](CLIENTS.md)                                                                  |
| Use Console from R                                                                                | [Use Console from R](R.md)                                                                 |
| Use Console from Python                                                                           | [Use Console from Python](PYTHON.md)                                                       |
| Install Console or check platform requirements                                                    | [Installation](INSTALLATION.md)                                                            |
| Run an analysis across R, Python, and SQL                                                         | [Sessions and languages](BUILTIN_RUNTIME.md)                                               |
| Install packages or use an existing environment                                                   | [Packages and environments](REQUIREMENTS.md)                                               |
| Customize the agent's tools and prepare its environment                                           | [Customize Console](BUILD_ON_CONSOLE.md)                                                   |
| Create a configuration file, understand which settings win, or inspect the resolved configuration | [Configuration files](CONFIGURATION.md)                                                    |
| Use a Python environment or select an R installation                                              | [Interpreters and startup](RUNTIME_CONFIGURATION.md)                                       |
| Choose startup packages or control when more may be prepared                                      | [Package declarations and policies](RUNTIME_CONFIGURATION.md#startup-package-declarations) |
| Connect to a database before the first SQL call                                                   | [Database startup](RUNTIME_CONFIGURATION.md#session-startup-source)                        |
| Choose what languages, parameters, or instructions the agent sees                                 | [Tool configuration](TOOL_CONFIGURATION.md)                                                |
| Allow project writes, protect files, or reach a service                                           | [Sandbox permissions](SANDBOX_CONFIGURATION.md)                                            |
| Use a package mirror or change cache locations                                                    | [Package sources and caches](RESOLVER.md)                                                  |
| Set environment variables or look up how Console handles them                                     | [Environment variables](ENVIRONMENT.md)                                                    |
| Look up language helpers, SQL connections, plotting, or output behavior                           | [Language behavior](RUNTIME_REFERENCE.md)                                                  |
| Retrieve a SQL result as an R or Python object                                                    | [Retrieve the last SQL result](RUNTIME_REFERENCE.md#retrieve-the-last-sql-result)          |
| Find recorded output or turn an analysis into a report                                            | [Recordings and reports](RECORDING.md)                                                     |
| Run a program with filesystem and network limits                                                  | [Sandbox command](SANDBOX_COMMAND.md)                                                      |
| Register a client, check an installation, or look up a command                                    | [Command-line reference](CLI.md) and [CLI help](cli-help.qmd)                              |

For a particular configuration key, use the [settings index](CONFIGURATION.md#settings-by-task).

## Learn Console {#learn-console}

<span id="using-console"></span> <span id="use-console"></span> <span id="work-with-console"></span> <span id="how-console-works"></span>

These pages introduce Console and show how its pieces fit together:

1. [Sessions and languages](BUILTIN_RUNTIME.md) shows how your agent can run code, query databases, and inspect help, plots, and results.
2. [Packages and environments](REQUIREMENTS.md) explains how Console prepares packages, adds dependencies during an analysis, and uses environments you already manage.
3. [Sandbox and trust](SANDBOX.md) explains where code runs, what it can access, and how session permissions differ from package-preparation permissions.
4. [Recordings and reports](RECORDING.md) explains how to inspect the agent's work and reuse its code, output, and plots.

The [glossary](GLOSSARY.md) defines terms used in these pages.

## Customize Console

[Customize Console](BUILD_ON_CONSOLE.md) shows how to choose the languages and parameters your agent sees, describe available data, and prepare a session with packages, connections, and helper functions.
These settings apply to all connection methods.

## Other documentation

<span id="reference"></span> <span id="configure-a-project"></span> <span id="public-reference"></span>

[API reference](REFERENCE.md) documents the MCP interface and the R and Python packages.

<span id="develop-console"></span>

[Development](DEVELOPMENT.md) is for changing Console itself, including architecture, internal protocols, testing, and release procedures.
