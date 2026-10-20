# Guides

Learn how Console's persistent workbench handles code, packages, permissions, and recorded results.
These guides apply whether you connect with an MCP client, from R, or from Python.

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

<span id="get-connected"></span> <span id="platform-setup"></span>

[Get started](getting-started.qmd) covers connecting a client and running a first analysis.
[Installation](INSTALLATION.md) covers platform requirements and runtime setup.

<span id="reference"></span> <span id="configure-a-project"></span> <span id="public-reference"></span>

[Reference](REFERENCE.md) organizes settings, commands, and APIs by task.
Use its "I want to…" table to find precise behavior and examples.

<span id="develop-console"></span>

[Development](DEVELOPMENT.md) is for changing Console itself, including architecture, internal protocols, testing, and release procedures.
